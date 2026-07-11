"""Vision + capture MCP tools: screenshot, get_interactive_elements, review_ui, verify_ui,
measure_ui, transcribe. They resolve the target/quality plan and delegate to the capture + vlm
helpers; the discover→verify→measure trio share the ``_run_ui_critique`` runner."""

import base64
import logging

from mcp.server.fastmcp.utilities.types import Image

from interact.config import DEFAULT_LIMIT
from interact.vision.critique import (
    UIReview,
    VerifyReport,
    build_review_prompt,
    build_verify_prompt,
    format_review,
    format_verify,
    parse_review,
    parse_verify,
)
from interact.debug_utils import Debug
from interact.desktop import DesktopElement
from interact.vision.detect import _crop_image, _desktop_context
from interact.vision.measure import format_measure, measure
from interact.models import is_audio_model, is_transcription_only_model
from interact.server import capture, core, targets, vlm
from interact.server.core import _DEFAULT_SESSION, _audio_mime, _session_response, config, instrumented, mcp
from interact.state import format_element_list
from interact.vision import analyze_media, transcribe_audio

_log = logging.getLogger("interact")


@mcp.tool()
@instrumented
async def screenshot(
    query: str | None = None,
    scope: str | None = None,
    selector: str | None = None,
    element: int | None = None,
    path: str | None = None,
    return_image: bool = False,
    debug_dir: str | None = None,
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
    model: str | None = None,
):
    """Capture the current page or a desktop window.

    Default (target unset): operates on browser session "default".
    target=<window title>: captures a desktop window. target="screen"/"screen:<index>": the whole
    desktop or one monitor (use list_desktop_windows to discover windows + monitor indexes).
    target="file:<path>": ANALYZE an existing image file (with query) instead of capturing — for an
    artifact produced out-of-band; this never writes, so it can't clobber the file.
    A desktop target and a non-default session are mutually exclusive.

    Returns depend on parameters:
    - No selector/element, no query: page title + visible text content (browser) or, for a
      desktop window, already-detected interactive elements as a numbered ref list if any exist
      (else metadata + a pointer to get_interactive_elements). screenshot never runs VLM grounding.
    - No selector/element, with query: full screenshot analyzed by VLM.
    - With selector/element, no query: element metadata (browser only).
    - With selector/element, with query: cropped element screenshot analyzed by VLM (browser only).

    element: integer index from get_interactive_elements (priority over selector).
    selector: CSS selector targeting one element (browser only).
    query: question for VLM visual analysis of the captured content.
    scope: CSS selector to restrict text extraction to a sub-tree (browser only).
    path: OUTPUT sink — saves the captured PNG here (overwrites any existing file, and says so). To
        ANALYZE an existing image, use target="file:<path>", not path.
    return_image: when True, return the raw screenshot bytes as an MCP ImageContent alongside the text,
        so the calling agent can SEE the pixels directly (not just a VLM summary).
    model: override the configured VLM model for this call. Uses the VS Code configured model when not set.
    """
    inv = Debug.inv()
    Debug.dump_input(inv, {"tool": "screenshot", "query": query, "scope": scope, "selector": selector,
                           "element": element, "target": target, "session": session, "model": model},
                     vlm._resolved_config(model, "image"))
    # target="file:<path>" analyzes an EXISTING image instead of capturing (no clobber, #44).
    file_bytes, ferr = targets._resolve_image_source(target)
    if ferr:
        return ferr
    if file_bytes is not None:
        src = target.strip()[5:]
        label = f"Image file: {src}"
        if query:
            r = await vlm._vlm(file_bytes, label, query, model_override=model)
            text = f"{label}\n{vlm._fmt_timing(r)}"
        else:
            import io as _io
            from PIL import Image as _PILImage
            w, h = _PILImage.open(_io.BytesIO(file_bytes)).size
            text = f"{label} ({w}x{h}) — pass query=… to analyze it, or use measure_ui for exact pixels."
        Debug.save("capture", file_bytes, ext="png", invocation_id=inv)
        out = [text, Image(data=file_bytes, format="png")] if return_image else text
        return out
    win, mgr, err = targets._resolve_target(target, session)
    if err:
        return err
    # If `path` already exists we're about to OVERWRITE it with this capture — surface that so the
    # result can't be mistaken for an analysis of the prior file (#44). To analyze a file, use
    # target="file:<path>" above; `path` is an OUTPUT sink.
    overwrote_path = bool(path) and __import__("pathlib").Path(path).exists()
    img_bytes: bytes | None = None
    if win:
        if element is not None:
            el = targets._resolve_desktop_el(win.wid, win.name, element=element)
            if el is None:
                nf = core._not_found(f"Element {element}")
                return nf
            raw = win.capture()
            img_bytes = _crop_image(raw, el.x, el.y, el.w, el.h)
            meta = f"[{el.index}] {el.role}: {el.name!r} ({el.w}x{el.h} at {el.x},{el.y})"
            result = await vlm._media_response(img_bytes, meta, query, path, model_override=model)
            text = f"{core._desktop_label(win)}\n{result or meta}"
        elif query:
            img_bytes, description = await capture._capture_desktop(win, query, path, model_override=model)
            text = f"{core._desktop_label(win)}\n{description}"
        else:
            # No query → just capture. screenshot NEVER runs VLM grounding (that's
            # get_interactive_elements' job, and a VLM call here would be slow + wrong). If a
            # detection already exists for this window, surface those refs so the capture is
            # actionable; otherwise return metadata and point the agent at the detect tool.
            img_bytes = win.capture()
            if path:
                core._save_to_path(path, img_bytes)
            # Surface cached refs ONLY if they belong to the frame just captured — after a navigation
            # the live frame's signature differs, so we don't list a prior screen's refs on a screen
            # that's no longer shown (the screenshot↔elements desync, #19).
            from interact.vision.detect import _page_signature
            cached = DesktopElement.cached_for(win.wid, _page_signature(img_bytes))
            if cached:
                text = f"{core._desktop_label(win)}\n{DesktopElement.format_list(cached)}"
            else:
                text = (
                    f"{core._desktop_label(win)}\n{_desktop_context(win)}\n"
                    "(call get_interactive_elements to detect clickable elements and act by [ref])"
                )
    elif element is not None or selector is not None:
        text = _session_response(
            session, await capture._element_screenshot(mgr, mgr.active_tab, selector, element, query, path)
        )
    else:
        state = await capture._capture(mgr, scope)
        img_bytes = base64.b64decode(state.screenshot_base64)
        if path:
            core._save_to_path(path, img_bytes)
        if query:
            text = _session_response(session, await vlm._analyze(state, query, model_override=model))
        else:
            # No query → no VLM. Surface the page's refs (pure DOM scan) so the capture is
            # actionable: the agent can click/type by `ref` without a follow-up detect call.
            elements = await capture._scan_elements(mgr, scope=scope)
            refs = (
                f"\n\nInteractive elements (act by ref in run_actions):\n"
                f"{format_element_list(elements)}"
                if elements
                else ""
            )
            text = _session_response(session, state.text_summary() + refs)
    if overwrote_path:
        text += f"\n(note: overwrote existing file {path} with this capture)"
    if img_bytes is not None:
        Debug.save("capture", img_bytes, ext="png", invocation_id=inv)
    result = [text, Image(data=img_bytes, format="png")] if (return_image and img_bytes is not None) else text
    return result


@mcp.tool()
@instrumented
async def get_interactive_elements(
    scope: str | None = None,
    query: str | None = None,
    element: int | None = None,
    limit: int = DEFAULT_LIMIT,
    tab: int | None = None,
    debug_dir: str | None = None,
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
    method: str = "default",
    model: str | None = None,
    fresh: bool = False,
) -> str:
    """List the interactive elements with numbered badges + their details; act on them by the
    returned `ref`/`element` in run_actions.

    Default (target unset): browser session "default" — sets data-interact-ref attributes via a
    pure DOM scan (no VLM). get_page_state and screenshot return these refs too, so you often
    already have them without a separate call. target=<window title>: VLM-detects elements in a
    desktop window;
    target="screen"/"screen:<index>": VLM-detects across the whole desktop or one monitor.
    A desktop target and a non-default session are mutually exclusive (list_desktop_windows lists them).

    Returns a numbered list with role/name for each element.
    Use element indices in subsequent click_element actions, or ref values for click/type_text/hover (browser only).
    scope: CSS selector to restrict to a page sub-tree (browser only).
    element: re-detect within a previously detected element's bounding box (crop and refine, window only).
    limit: Maximum number of elements to return (browser only).
    With query, also returns a vision analysis of the annotated screenshot.
    debug_dir: when set, dump inputs/outputs/screenshots to this directory for debugging.
    method: detection strategy — "default" (AT-SPI with VLM fallback) or "vlm" (force VLM only). Applies to desktop windows.
    model: override the configured VLM model for this call. Uses the VS Code configured model when not set.
    fresh: desktop/nested only — force-clear this window's accumulated element cache before detecting,
        so the returned refs reflect ONLY the current frame. Use it to recover if clicks stop landing
        or the ref list looks stale/duplicated after many interactions (#57).
    """
    inv = Debug.inv()
    Debug.dump_input(inv, {"tool": "get_interactive_elements", "query": query, "scope": scope,
                           "element": element, "limit": limit, "tab": tab, "target": target,
                           "session": session, "method": method, "model": model},
                     vlm._resolved_config(model, "component"))
    win, mgr, err = targets._resolve_target(target, session)
    if err:
        return err
    if win:
        if fresh:
            DesktopElement.invalidate(win.wid)  # #57: start from the live frame, drop stale refs
        crop = None
        if element is not None:
            el = targets._resolve_desktop_el(win.wid, win.name, element=element)
            if el is None:
                nf = core._not_found(f"Element {element}")
                return nf
            crop = (el.x, el.y, el.w, el.h)
        _, report = await capture._annotate_desktop(
            win, query, crop, invocation_id=inv, method=method, model_override=model
        )
        result = f"{core._desktop_label(win)}\n{report}"
    else:
        result = _session_response(
            session, await capture._annotate_and_describe(mgr, tab, scope, query, limit)
        )
    _log.info("get_interactive_elements: %s", "desktop" if win else "browser")
    return result


@mcp.tool()
async def review_ui(
    focus: str | None = None,
    reference: str | None = None,
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
    scope: str | None = None,
    path: str | None = None,
    model: str | None = None,
    quality: str | None = None,
) -> str:
    """Capture the UI and return a STRUCTURED critique of what's WRONG with it — low-contrast or
    unreadable text, overflow/clipping, truncation, misalignment, broken/empty/error states, black or
    occluded regions, tiny tap targets, off-theme colors — so you can JUDGE a UI's quality without
    hand-writing a vision prompt. Use it after a change to confirm the result looks right, or to hunt
    defects on any screen.

    Findings come back severity-sorted, one per line as `[critical|major|minor]/<category> location:
    issue → fix`; a clean screen reports no defects. Works on any target like screenshot: unset/
    "browser" = the browser session, a window title = a desktop window, "screen"/"nested[:title]" =
    the sandbox or whole desktop.

    focus: optional extra emphasis (e.g. "the background should be warm sand, not purple"; "check the
        bottom nav isn't black/occluded") — narrows the review WITHOUT replacing the built-in rubric.
    reference: path to a reference/target image (a design or a prior good build). When set, the review
        judges how the capture DIVERGES from this reference (wrong accent, missing nav, layout drift),
        instead of against a generic ideal — the reliable way to catch a build that's subtly off.
    path: save the reviewed PNG here. Requires a configured vision model (same as screenshot's query).
    quality: pick the model by STAKES, not by name — "low"/"medium" use a cheap sovereign self-host
        model, "high"/"critical" the best frontier model; "critical" also drops findings whose element
        interact can't confirm (highest precision, for a pre-ship sign-off). Unset = the configured/
        auto model. An explicit model= still overrides this.
    """
    return await capture._run_ui_critique(
        tool="review_ui",
        dump_extra={"focus": focus, "session": session},
        build_prompt=lambda compare, grounding: build_review_prompt(focus, compare=compare, grounding=grounding),
        schema=UIReview,
        parse=parse_review,
        apply_strict=capture._review_drop_phantom_findings,
        format_body=format_review,
        target=target, session=session, scope=scope, path=path,
        reference=reference, model=model, quality=quality,
    )


@mcp.tool()
async def verify_ui(
    requirements: list[str],
    target: str | None = None,
    reference: str | None = None,
    focus: str | None = None,
    session: str = _DEFAULT_SESSION,
    scope: str | None = None,
    path: str | None = None,
    model: str | None = None,
    quality: str | None = None,
) -> str:
    """Judge a UI against your LITERAL requirements — one PASS/FAIL per requirement, each anchored to
    the exact element it judged. The acceptance complement to review_ui (which DISCOVERS defects): hand
    it the checklist a freeform critique glosses ("the coin pill shows a GOLD coin, not a flame"; "the
    bottom nav has exactly 4 tabs"; "the FAB does not overlap the tab bar") and it tests each to the
    letter — presence is not enough, the form/color/count/state must match.

    Captures like review_ui — target unset/"browser" = the page; a window title; "screen";
    "nested[:title]"; or "file:<path>" to verify a saved image. Pass a reference image to judge each
    requirement against a target design. For a hard form-defect, confirm the number with measure_ui.

    requirements: the literal requirements to check, each judged PASS / FAIL / UNCLEAR with evidence.
    focus: optional extra emphasis layered onto the rubric.
    reference: a target/design image to judge the build against.
    quality: pick the model by STAKES — "low"/"medium" use a cheap sovereign model, "high"/"critical"
        the best frontier; "critical" downgrades any PASS resting on an element interact can't confirm.
        Unset = configured/auto. An explicit model= overrides this.
    """
    if not requirements:
        return "ERROR: verify_ui needs at least one requirement to check."
    return await capture._run_ui_critique(
        tool="verify_ui",
        dump_extra={"requirements": requirements, "focus": focus},
        build_prompt=lambda compare, grounding: build_verify_prompt(requirements, focus, compare=compare, grounding=grounding),
        schema=VerifyReport,
        parse=parse_verify,
        apply_strict=capture._verify_downgrade_phantom_pass,
        format_body=format_verify,
        target=target, session=session, scope=scope, path=path,
        reference=reference, model=model, quality=quality,
    )


@mcp.tool()
@instrumented
async def measure_ui(
    target: str | None = None,
    region: str | None = None,
    point: str | None = None,
    session: str = _DEFAULT_SESSION,
    scope: str | None = None,
    path: str | None = None,
) -> str:
    """DETERMINISTIC pixel measurement of a UI — exact colors + WCAG contrast, NO VLM (no spend, fully
    reproducible). Use it for a number you can trust instead of a model's guess: the contrast ratio of
    text vs background, the exact color at a point, or the biggest empty band on screen.

    Captures like screenshot/review_ui — target unset/"browser" = the page; a window title; "screen";
    "nested[:title]"; or "file:<path>" to measure an existing image. Then:
    - region="x,y,w,h": dominant colors in that box, the two-color WCAG contrast ratio (PASS/FAIL for
      AA-normal 4.5, AA-large 3.0, AAA 7.0), and the largest uniform band inside it.
    - point="x,y": the exact color (hex) at that pixel.
    - neither: whole-image palette + the largest uniform (empty) band.

    Pairs with review_ui: the VLM flags a suspect ("this text looks low-contrast") → measure_ui
    confirms the actual ratio. Coordinates are image pixels (as screenshot / get_interactive_elements
    report them).
    """
    inv = Debug.inv()
    Debug.dump_input(inv, {"tool": "measure_ui", "target": target, "region": region,
                           "point": point, "session": session})
    reg = core._parse_int_tuple(region, 4, "region")
    if isinstance(reg, str):
        return reg
    pt = core._parse_int_tuple(point, 2, "point")
    if isinstance(pt, str):
        return pt
    img, label, _mgr, _win, err = await capture._capture_or_file(target, session, scope)
    if err:
        return err
    if path:
        core._save_to_path(path, img)
    try:
        result = measure(img, region=reg, point=pt)
    except Exception as e:
        return f"ERROR: measure_ui failed — {e}"
    out = f"{label}\n{format_measure(result)}"
    return out


@mcp.tool()
async def transcribe(path: str, query: str | None = None, model: str | None = None) -> str:
    """Transcribe an audio (or audio-bearing) file to text, and optionally answer a question about it.

    Point it at a local file `path` — a clip you grabbed with download_asset, or a recording you
    saved with record(path=...). Accepts mp3/wav/m4a/webm/ogg/flac and mp4/mov (the audio track is
    used). Returns the transcript; with `query`, returns an answer about the audio instead.

    Audio understanding is acoustic (it HEARS the clip — tone, speakers, music, sound events) when the
    audio model can take audio in chat (Gemini, gpt-4o-audio); with a transcription-only model
    (Whisper, gpt-4o-transcribe) the query is answered over the transcript. Set the model with the
    `audio.model` setting / INTERACT_AUDIO_MODEL, or override per-call with `model`.

    path: local audio/media file to read.
    query: optional question about the audio (omit for a plain transcript).
    model: override the configured audio model for this call.
    """
    from pathlib import Path

    config.refresh()
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        return f"ERROR: could not read audio file {path!r} — {e}"
    mime = _audio_mime(path)
    audio_model = config.resolve_model("audio", model or "")
    name = Path(path).name

    # Acoustic understanding when the model can hear the clip directly; otherwise fall through to
    # transcript-based answering below (so Whisper-style transcription-only models still serve a query).
    if query and is_audio_model(audio_model) and not is_transcription_only_model(audio_model):
        try:
            r = await vlm._vlm(data, f"Audio file: {name}", query, "audio", mime, model_override=audio_model)
            return vlm._fmt_timing(r)
        except Exception as e:
            return f"ERROR: audio understanding failed on {audio_model} — {e}"

    try:
        r = await transcribe_audio(data, model=audio_model, mime_type=mime)
    except Exception as e:
        return f"ERROR: transcription failed on {audio_model} — {e}"
    transcript = r.text
    if not query:
        return f"{transcript}\n(transcribed:{(' ' + r.model) if r.model else ''} {r.elapsed:.1f}s)"

    answer = await analyze_media(
        [], f"Transcript of {name}:\n{transcript}", config, query, model=config.resolve_model("image")
    )
    return f"{answer.text}\n\n--- transcript ---\n{transcript}\n(VLM: {answer.model} {answer.elapsed:.1f}s)"
