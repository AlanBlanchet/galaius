"""Capture + annotation + the shared UI-critique flow. Browser page capture, DOM-ref scanning,
desktop-window capture/detection, per-target PNG bytes, and the review_ui/verify_ui runner all
live here — the machinery the vision tools compose, apart from the tools themselves."""

import base64
import logging

from interact.config import DEFAULT_LIMIT, QUALITY_TIERS
from interact.vision.critique import format_grounding
from interact.debug_utils import Debug
from interact.desktop import DesktopElement, DesktopWindow
from interact.vision.detect import _desktop_context, _detect_desktop_elements
from interact.server import core, targets, vlm
from interact.server.core import config
from interact.state import (
    InteractiveElement,
    PageState,
    annotate_screenshot,
    format_element_list,
)

_log = logging.getLogger("interact")


async def _capture(mgr, scope: str | None = None, tab: int | None = None):
    page = await mgr.get_page(tab)  # tab=None → the session's active tab (#30)
    return await PageState.capture(page, scope=scope)


async def _scan_elements(
    mgr,
    tab: int | None = None,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[InteractiveElement]:
    """The DOM scan behind every browser ref — pure ``page.evaluate`` over the page's own DOM,
    NO VLM. It sets ``data-interact-ref`` attributes and returns the elements, so refs are
    model-agnostic (they work with any configured model, or none) and free to surface widely.
    The element map is registered so a following run_actions can act by these refs."""
    page = await mgr.get_page(tab)
    result = await page.evaluate(
        core._ANNOTATE_JS, {"scope": scope, "limit": limit, "nextRef": mgr._ref_counter}
    )
    mgr._ref_counter = result["nextRef"]  # advance the session's monotonic ref counter (#35)
    elements = [
        InteractiveElement(
            index=int(raw["ref"][1:]),  # ref "eN" ↔ index N, both stable across scans in a session
            ref=raw["ref"],
            role=raw["tag"],
            name=raw["name"],
            x=raw["x"],
            y=raw["y"],
            width=raw["width"],
            height=raw["height"],
        )
        for raw in result["elements"]
    ]
    mgr.set_element_map(tab, elements)
    return elements


async def _annotate_page(
    mgr,
    tab: int = 0,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[bytes, list[InteractiveElement]]:
    elements = await _scan_elements(mgr, tab, scope, limit)
    page = await mgr.get_page(tab)
    screenshot_bytes = await page.screenshot(type="png")
    return annotate_screenshot(screenshot_bytes, elements), elements


async def _annotate_and_describe(
    mgr,
    tab: int = 0,
    scope: str | None = None,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    annotated_bytes, elements = await _annotate_page(mgr, tab, scope, limit)
    element_list = format_element_list(elements)
    context = f"Annotated page with {len(elements)} interactive elements:\n{element_list}"
    result = await vlm._media_response(annotated_bytes, context, query)
    return result or context


async def _capture_desktop(
    win: DesktopWindow,
    query: str | None = None,
    path: str | None = None,
    model_override: str | None = None,
) -> tuple[bytes, str]:
    screenshot_bytes = win.capture()
    context = _desktop_context(win)
    result = await vlm._media_response(
        screenshot_bytes, context, query, path, model_override=model_override
    )
    return screenshot_bytes, result or context


async def _annotate_desktop(
    win: DesktopWindow,
    query: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
    invocation_id: str | None = None,
    method: str = "default",
    model_override: str | None = None,
) -> tuple[list[DesktopElement] | None, str]:
    (
        screenshot_bytes,
        elements,
        vlm_raw,
        elapsed,
        method_label,
        img_w,
        img_h,
    ) = await _detect_desktop_elements(
        win,
        crop,
        invocation_id=invocation_id,
        method=method,
        model_override=model_override,
        query=query,
    )
    parts = [f"method={method_label}"]
    parts.extend([f"{len(elements)} elements", f"{elapsed:.2f}s"])
    timing = f"Detection: {' | '.join(parts)}"
    if not elements:
        detail = f"VLM response:\n{vlm_raw}" if vlm_raw else "No elements detected"
        return None, f"Could not detect elements. {timing}\n{detail}"
    try:
        if crop:
            ann_elements = [el.translate(-crop[0], -crop[1]) for el in elements]
        else:
            ann_elements = elements
        annotated = annotate_screenshot(screenshot_bytes, ann_elements)
    except Exception:
        _log.warning(
            "annotate_desktop: failed to generate annotated image (%d elements)",
            len(elements),
            exc_info=True,
        )
        element_list = DesktopElement.format_list(elements)
        return elements, f"Elements detected but annotation failed.\n{element_list}\n{timing}"
    Debug.save("annotated", annotated, ext="png", invocation_id=invocation_id)
    element_list = DesktopElement.format_list(elements)
    context = f"Annotated desktop window with {len(elements)} elements:\n{element_list}"
    result = await vlm._media_response(annotated, context, query, model_override=model_override)
    return elements, f"{result or context}\n{timing}"


async def _element_screenshot(
    mgr,
    tab: int,
    selector: str | None,
    element: int | None,
    query: str | None = None,
    path: str | None = None,
) -> str:
    page = await mgr.get_page(tab)

    if element is not None:
        el = mgr.get_element(element, tab)
        if el is None:
            return core._not_found(f"Element {element}")
        if not el.playwright_ref:
            return f"Element {element} has no ref attribute — cannot screenshot"
        locator = page.locator(el.playwright_ref)
        meta = f"[{el.index}] {el.role}: {el.name!r} ({el.width:.0f}x{el.height:.0f} at {el.x:.0f},{el.y:.0f})"
    else:
        locator = page.locator(selector)
        count = await locator.count()
        if count == 0:
            return f"No element matches '{selector}'"
        if count > 1:
            return f"'{selector}' matches {count} elements — use get_interactive_elements and element for precision"
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        text = (await locator.inner_text())[:200]
        box = await locator.bounding_box()
        meta = f"{tag}: {text!r}"
        if box:
            meta += f" ({box['width']:.0f}x{box['height']:.0f} at {box['x']:.0f},{box['y']:.0f})"

    try:
        png_bytes = await locator.screenshot(type="png")
    except Exception as e:
        return f"Cannot screenshot element: {e}"
    result = await vlm._media_response(png_bytes, meta, query, path)
    return result or meta


async def _wait(page, condition: str | None):
    """A load state waits for it; a bare number (or "Ns") sleeps that many seconds — agents keep
    passing wait="3" meaning seconds, and parsing it as a CSS selector throws (#63); anything else
    is a CSS selector waited to visibility."""
    import asyncio

    if condition is None:
        return
    if condition in ("networkidle", "domcontentloaded", "load"):
        await page.wait_for_load_state(condition)
        return
    try:
        await asyncio.sleep(float(condition.strip().rstrip("s")))
        return
    except ValueError:
        pass
    await page.wait_for_selector(condition, state="visible", timeout=config.wait_timeout)


async def _capture_target_png(win: DesktopWindow | None, mgr, scope: str | None) -> bytes:
    """PNG bytes for a resolved target — a desktop window/screen (its own capture) or the browser page."""
    if win:
        return win.capture()
    state = await _capture(mgr, scope)
    return base64.b64decode(state.screenshot_base64)


async def _resolve_capture(target, session, scope, path, reference, inv):
    """Shared capture path for review_ui / verify_ui: a ``file:<path>`` target or a live capture,
    saved to ``path`` if given, plus an optional ``reference`` image. Returns
    ``(img_bytes, context, ref_bytes, elements, err_or_None)`` — on error the caller returns the string.

    ``elements`` is interact's detected element list for a BROWSER target (the reliable, no-VLM DOM-ref
    scan), used to GROUND the critique and flag a hallucinated ref. Empty for a desktop/file target,
    where no equally-reliable list exists."""
    from pathlib import Path

    file_bytes, ferr = targets._resolve_image_source(target)  # target="file:<path>" → judge a saved image (#44)
    if ferr:
        return None, None, None, [], ferr
    elements: list = []
    if file_bytes is not None:
        img, context = file_bytes, f"Image file: {target.strip()[5:]}"
    else:
        win, mgr, err = targets._resolve_target(target, session)
        if err:
            return None, None, None, [], err
        img = await _capture_target_png(win, mgr, scope)
        context = core._desktop_label(win) if win else "Browser page"
        if win is None and mgr is not None:  # browser target → DOM ref list to anchor the critique on
            try:
                elements = await _scan_elements(mgr, scope=scope)
            except Exception:
                elements = []  # never fail a capture because the grounding scan hiccuped
    if path:
        core._save_to_path(path, img)
    Debug.save("capture", img, ext="png", invocation_id=inv)
    ref_bytes = None
    if reference:
        try:
            ref_bytes = Path(reference).read_bytes()
        except OSError as e:
            return None, None, None, [], f"ERROR: could not read reference image {reference!r} — {e}"
    return img, context, ref_bytes, elements, None


def _quality_plan(quality: str | None, model: str | None) -> tuple[str | None, bool, str | None]:
    """Resolve a quality tier to ``(effective_model, strict, error)``. An explicit ``model`` wins over
    the tier's model. ``strict`` (critical only) drops a finding citing a ref the scan never detected —
    a deterministic precision boost for a pre-ship sign-off. ``error`` is set on an unknown tier."""
    if quality is None:
        return model or None, False, None
    q = quality.strip().lower()
    if q not in QUALITY_TIERS:
        return None, False, f"ERROR: quality must be one of {', '.join(QUALITY_TIERS)} (got {quality!r})"
    return (model or config.resolve_quality_model(q) or None), q == "critical", None


def _review_drop_phantom_findings(review, valid_refs) -> None:
    """critical mode: drop a finding that cites an element interact can't confirm (a phantom ref)."""
    review.findings = [f for f in review.findings if not (f.ref and f.ref not in valid_refs)]


def _verify_downgrade_phantom_pass(report, valid_refs) -> None:
    """critical mode: a PASS resting on a phantom ref can't stand → downgrade it to unclear."""
    for c in report.checks:
        if c.ref and c.ref not in valid_refs and c.verdict == "pass":
            c.verdict = "unclear"
    report.all_pass = all(c.verdict == "pass" for c in report.checks)


async def _run_ui_critique(
    *, tool: str, dump_extra: dict, build_prompt, schema, parse, apply_strict, format_body,
    target, session, scope, path, reference, model, quality,
) -> str:
    """The shared review_ui / verify_ui flow — capture → ground → VLM (reference-first when a
    reference is given) → parse → strict-filter → format. The two tools differ only in the prompt
    builder, the response schema, the parser/formatter, and the strict-mode filter, all injected
    here; everything else was byte-identical between them."""
    config.refresh()
    eff_model, strict, qerr = _quality_plan(quality, model)
    if qerr:
        return qerr
    inv = Debug.new_invocation_dir(None, tool)
    Debug.dump_input(inv, {"tool": tool, "target": target, "reference": reference,
                           "model": model, "quality": quality, **dump_extra},
                     vlm._resolved_config(eff_model, "image"))
    img, context, ref_bytes, elements, err = await _resolve_capture(
        target, session, scope, path, reference, inv
    )
    if err:
        Debug.dump_output(inv, err)
        return err
    grounding = format_grounding(elements) if elements else None  # anchor findings to real elements
    valid_refs = {e.ref for e in elements if getattr(e, "ref", None)} or None
    try:
        if ref_bytes is not None:  # reference first, build second — matches the compare rubric
            r = await vlm._vlm(ref_bytes, context, build_prompt(True, grounding),
                               response_format=schema, model_override=eff_model, extra_images=[img])
        else:
            r = await vlm._vlm(img, context, build_prompt(False, grounding),
                               response_format=schema, model_override=eff_model)
    except Exception as e:  # never crash the agent's flow on a vision hiccup
        return f"ERROR: {tool} vision call failed — {e}"
    parsed = parse(r.text)
    if parsed and strict and valid_refs is not None:
        apply_strict(parsed, valid_refs)
    body = format_body(parsed, valid_refs) if parsed else r.text  # graceful: raw VLM text on parse miss
    model_tag = f" {r.model}" if r.model else ""
    out = f"{context}\n{body}\n(VLM:{model_tag} {r.elapsed:.1f}s)"
    Debug.dump_output(inv, out)
    return out
