import asyncio
import base64
import hashlib
import io
import json
import logging
import time

import litellm as _litellm
from PIL import Image as PILImage
from pydantic import BaseModel

from interact.desktop.atspi import AtSpi
from interact.debug_utils import Debug
from interact.desktop import (
    CoordTransform,
    DesktopElement,
    DesktopWindow,
    _IOU_OVERLAP_THRESHOLD,
    _TITLEBAR_Y,
    _WM_BUTTON_NAMES,
)
from interact.formats import CoordFormat
from interact.runtime import breaker, config
from interact.state import annotate_screenshot
from interact.vision.core import MediaItem, analyze_media

_log = logging.getLogger("interact")

_PARTIAL_ATSPI_THRESHOLD = 3
_LOW_ELEMENT_WARN = 5


class _DetectedElement(BaseModel):
    name: str
    role: str
    x: int
    y: int
    w: int
    h: int


class _DetectionResult(BaseModel):
    elements: list[_DetectedElement]


def _model_supports_structured(model: str) -> bool:
    return _litellm.supports_response_schema(model=model)


def _structured_to_elements(result: _DetectionResult) -> list[DesktopElement]:
    return [
        DesktopElement(
            index=i + 1,
            x=det.x,
            y=det.y,
            w=det.w,
            h=det.h,
            role=det.role,
            name=det.name,
        )
        for i, det in enumerate(result.elements)
    ]


async def _vlm_detect_elements(
    screenshot_bytes: bytes,
    context: str,
    img_w: int,
    img_h: int,
    crop_offset: tuple[int, int, int, int] | None = None,
    invocation_id: str | None = None,
    simple: bool = False,
    model_override: str | None = None,
) -> tuple[list[DesktopElement] | None, float, str, str]:
    # Sourced from the split server submodules so a test patching srv.vlm._vlm / srv.core.config
    # is seen here (the server package imports detect, so this stays a lazy in-body import).
    from interact.server import core as _srv_core, vlm as _srv_vlm  # noqa: PLC0415

    _vlm = _srv_vlm._vlm
    config = _srv_core.config

    transform = CoordTransform.for_resize(
        img_w, img_h, config.vlm_max_dim, config.vlm_min_dim
    )
    if crop_offset:
        transform = transform.with_crop(crop_offset[0], crop_offset[1])
    vlm_bytes, vlm_w, vlm_h = transform.resize_image(screenshot_bytes, img_w, img_h)

    # Resolve each role to a concrete model at this boundary via the one resolution site
    # (Config.resolve_model): the configured id, else the first available model in the role's
    # chain. Without it an unconfigured (auto) install ran detection with an empty-string model
    # → instant silent failure (0 elements, 0.00s).
    component_model = config.resolve_model("component", breaker=breaker)
    image_model = config.resolve_model("image", breaker=breaker)

    if model_override:
        use_component = False
        detection_model = model_override
    else:
        use_component = (
            not simple
            and component_model
            and component_model != image_model
            and not breaker.tripped(component_model)
        )
        detection_model = component_model if use_component else image_model

    tasks: list = []
    task_labels: list[str] = []
    task_structured: list[bool] = []
    task_coord_formats: list[CoordFormat] = []

    def _enqueue(model: str, label: str):
        fmt = CoordFormat.for_model(model)
        structured = (
            not simple and fmt == CoordFormat() and _model_supports_structured(model)
        )
        preamble = (
            f"This image is {vlm_w}x{vlm_h} pixels. "
            "Detect all interactive UI elements (buttons, inputs, links, tabs, dropdowns, toggles, selectors, icon-buttons). "
            "Scan the ENTIRE image including toolbars, status bars, panel footers. "
        )
        prompt = preamble + fmt.prompt(vlm_w, vlm_h)
        resp_fmt = _DetectionResult if structured else None
        tasks.append(
            _vlm(
                vlm_bytes,
                context,
                prompt,
                media_type=label,
                max_tokens=None,
                response_format=resp_fmt,
                # Pass the RESOLVED detection model, not the outer model_override: in auto mode
                # model_override is None, and forwarding it made _vlm fall back to the (empty)
                # configured image model → an empty-string model id → instant silent failure
                # (0 elements, 0.00s). `model` is already the resolved component/image/override.
                model_override=model,
            )
        )
        task_labels.append(label)
        task_structured.append(structured)
        task_coord_formats.append(fmt)

    if model_override:
        _enqueue(detection_model, "override")
    elif use_component:
        _enqueue(component_model, "component")
    else:
        _enqueue(image_model, "image")

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_elements: list[DesktopElement] = []
    for label, r, is_structured, coord_format in zip(
        task_labels, results, task_structured, task_coord_formats
    ):
        if isinstance(r, BaseException) or r.text.startswith("[Vision"):
            _log.warning(
                "VLM detection failed (%s): %s",
                label,
                r if isinstance(r, BaseException) else r.text,
            )
            if label == "component":
                breaker.trip(component_model)
            continue
        parsed = None
        if is_structured:
            try:
                detection = _DetectionResult.model_validate_json(r.text)
                parsed = _structured_to_elements(detection)
                if parsed and all(el.x == 0 for el in parsed):
                    _log.warning("Structured output garbage (all x=0), discarding")
                    breaker.trip(
                        component_model if label == "component" else image_model
                    )
                    parsed = None
            except Exception:
                _log.warning(
                    "Structured parse failed (%s), falling back to text", label
                )
                parsed = coord_format.parse(r.text, vlm_w, vlm_h)
        else:
            parsed = coord_format.parse(r.text, vlm_w, vlm_h)
        if parsed:
            for el in parsed:
                if not any(
                    el.iou(existing) > _IOU_OVERLAP_THRESHOLD
                    for existing in all_elements
                ):
                    all_elements.append(el)

    all_elements = DesktopElement.merge_fragments(all_elements)

    all_elements = [el.transform(transform) for el in all_elements]

    clamped: list[DesktopElement] = []
    for el in all_elements:
        clamped_el = el.clamp(img_w, img_h)
        if clamped_el:
            clamped.append(clamped_el)
    all_elements = clamped

    for i, el in enumerate(all_elements):
        el.index = i + 1

    elapsed = max(
        (r.elapsed for r in results if not isinstance(r, BaseException)), default=0
    )
    raw_text = "\n---\n".join(
        r.text
        for r in results
        if not isinstance(r, BaseException) and not r.text.startswith("[Vision")
    )

    Debug.save("vlm_raw", raw_text, invocation_id=invocation_id)
    vlm_label = model_override or (component_model if use_component else image_model)
    Debug.save(
        "vlm_meta",
        json.dumps(
            {
                "model": vlm_label,
                "elements": len(all_elements),
                "elapsed_s": round(elapsed, 3),
                "vlm_resize": [vlm_w, vlm_h],
                "original_size": [img_w, img_h],
                "scale": [transform.scale_x, transform.scale_y],
                "coord_format": task_coord_formats[0].meta()
                if task_coord_formats
                else None,
            },
            indent=2,
        ),
        ext="json",
        invocation_id=invocation_id,
    )

    if all_elements and len(all_elements) < _LOW_ELEMENT_WARN and not crop_offset:
        _log.warning(
            "Low element count (%d) — possible prompt/format mismatch",
            len(all_elements),
        )

    return all_elements or None, elapsed, raw_text, vlm_label


async def judge_missing_elements(
    screenshot_bytes: bytes,
    elements: list[DesktopElement],
    model_override: str | None = None,
) -> list[str]:
    """VLM judge for detection completeness — "did we miss any components?".

    Draws the already-detected boxes in one colour and asks the model which
    interactive elements are still un-boxed; the uniform colour makes the covered
    set read as one layer so a missed element (a drop zone) stands out. Drives the
    :attr:`Config.detection_max_retries` re-detection loop. Returns the visible
    labels of missed elements (empty when nothing is missing).
    """
    annotated = annotate_screenshot(screenshot_bytes, elements, uniform_color="#00C000")
    media = [MediaItem(data=base64.b64encode(annotated).decode())]
    prompt = (
        "The green boxes mark interactive elements already found. List any OTHER "
        "interactive elements that are NOT boxed — buttons, inputs, links, draggable "
        "items, drop zones, toggles, menus. Reply with a short comma-separated list of "
        "their visible labels, or exactly NONE if every interactive element is boxed."
    )
    result = await analyze_media(
        media,
        "detection completeness check",
        config,
        prompt,
        model=config.resolve_model("component", model_override or ""),
    )
    text = (result.text or "").strip()
    if not text or text.upper().startswith("NONE") or text.startswith("["):
        return []
    labels = [part.strip(" .-•\t") for part in text.replace("\n", ",").split(",")]
    return [label for label in labels if label][:8]


def _is_wm_only(elements: list[DesktopElement], titlebar_y: int = _TITLEBAR_Y) -> bool:
    return bool(elements) and all(
        e.name in _WM_BUTTON_NAMES and e.role == "push button" and e.y < titlebar_y
        for e in elements
    )


def _page_signature(png_bytes: bytes) -> str:
    """Content fingerprint of the window screenshot — the key for "is this still the same screen?".
    Downscaled + grayscaled so the tiniest pixel diffs wash out; any real content change yields a
    new key. It errs toward a NEW key (re-detect) over a stale match — the safe direction. Must NOT
    be the window title: a single-window app (Flutter, Electron, a game) keeps one title across
    every screen, so a title key never resets and stale refs from the previous screen pile up."""
    try:
        thumb = PILImage.open(io.BytesIO(png_bytes)).convert("L").resize((16, 16))
        return hashlib.sha1(thumb.tobytes()).hexdigest()
    except Exception:  # never let signing a screenshot break detection
        return hashlib.sha1(png_bytes).hexdigest()


async def _detect_desktop_elements(
    win: DesktopWindow,
    crop: tuple[int, int, int, int] | None = None,
    invocation_id: str | None = None,
    method: str = "default",
    model_override: str | None = None,
    query: str | None = None,
) -> tuple[bytes, list[DesktopElement], str | None, float, str, int, int]:
    """Detect interactive elements via AT-SPI or VLM fallback.

    crop: (x, y, w, h) to restrict detection to a sub-region.
    method: "default" (AT-SPI with VLM fallback) or "vlm" (force VLM only). Applies to desktop windows.
    Returns (screenshot_bytes, elements, error_detail, elapsed_seconds, method_label, img_w, img_h).
    """
    import interact.server as _srv  # noqa: PLC0415 — circular: server imports detect

    screenshot_bytes = win.capture()
    offsets = CoordTransform.from_xprop(win.wid)
    CoordTransform.store(win.wid, offsets)
    if (
        offsets.shadow_left
        or offsets.shadow_top
        or offsets.shadow_right
        or offsets.shadow_bottom
    ):
        _img = PILImage.open(io.BytesIO(screenshot_bytes))
        raw_w, raw_h = _img.size
        visible_w = raw_w - offsets.shadow_left - offsets.shadow_right
        visible_h = raw_h - offsets.shadow_top - offsets.shadow_bottom
        if visible_w > 0 and visible_h > 0 and raw_w == win.w and raw_h == win.h:
            _log.debug(
                "Cropping CSD shadow frame: %dx%d → %dx%d (shadow l=%d t=%d r=%d b=%d)",
                raw_w,
                raw_h,
                visible_w,
                visible_h,
                offsets.shadow_left,
                offsets.shadow_top,
                offsets.shadow_right,
                offsets.shadow_bottom,
            )
            screenshot_bytes = _srv._crop_image(
                screenshot_bytes,
                offsets.shadow_left,
                offsets.shadow_top,
                visible_w,
                visible_h,
            )
    # Sign the full visible window BEFORE any region crop, so region-refinement passes on the same
    # screen share one signature (and accumulate), while a navigated-to screen gets a new one.
    page_sig = _page_signature(screenshot_bytes)
    if crop:
        screenshot_bytes = _srv._crop_image(screenshot_bytes, *crop)

    if crop:
        img_w, img_h = crop[2], crop[3]
    else:
        _img = PILImage.open(io.BytesIO(screenshot_bytes))
        img_w, img_h = _img.size
    context = _desktop_context(win)
    if query:
        # Bias detection toward the elements the caller is after, so the boxes land on
        # what they asked for (and a follow-up query can surface things a first pass missed).
        context += f"\n\nFocus on and prioritise interactive elements matching: {query}"

    Debug.save(
        "window_geometry",
        json.dumps(
            {
                "window": win.name,
                "wid": win.wid,
                "xwininfo_size": [win.w, win.h],
                "screenshot_size": [img_w, img_h],
                "offsets": offsets.model_dump(),
            },
            indent=2,
        ),
        ext="json",
        invocation_id=invocation_id,
    )

    t0 = time.monotonic()
    titlebar_y = offsets.decoration_top or _TITLEBAR_Y
    atspi_result = None if method == "vlm" else AtSpi.detect_elements(win.name)
    if atspi_result is not None and _is_wm_only(atspi_result, titlebar_y):
        _log.info(
            "detect_elements: atspi returned only WM buttons (%d), falling back to VLM",
            len(atspi_result),
        )
        atspi_result = None
    if atspi_result is not None:
        elapsed = time.monotonic() - t0
        Debug.save(
            "method",
            json.dumps(
                {
                    "method": "atspi",
                    "elements_count": len(atspi_result),
                    "elapsed_s": round(elapsed, 3),
                },
                indent=2,
            ),
            ext="json",
            invocation_id=invocation_id,
        )
        Debug.save(
            "input_screenshot",
            screenshot_bytes,
            ext="png",
            invocation_id=invocation_id,
        )
        if len(atspi_result) < _PARTIAL_ATSPI_THRESHOLD:
            _log.info(
                "detect_elements: atspi partial (%d elements), running VLM for fusion",
                len(atspi_result),
            )
            vlm_elements, _, _, vlm_label = await _vlm_detect_elements(
                screenshot_bytes,
                context,
                img_w,
                img_h,
                crop_offset=crop,
                invocation_id=invocation_id,
                model_override=model_override,
            )
            if vlm_elements:
                fused = DesktopElement.fuse(vlm_elements, atspi_result)
                fused = DesktopElement.merge_into(win.wid, fused, page_sig)
                total_elapsed = time.monotonic() - t0
                _log.info(
                    "detect_elements: fused %d elements (atspi=%d, vlm=%d) in %.3fs",
                    len(fused),
                    len(atspi_result),
                    len(vlm_elements),
                    total_elapsed,
                )
                return (
                    screenshot_bytes,
                    fused,
                    None,
                    total_elapsed,
                    f"fused+{vlm_label}",
                    img_w,
                    img_h,
                )
        atspi_result = DesktopElement.merge_into(win.wid, atspi_result, page_sig)
        _log.info(
            "detect_elements: atspi %d elements in %.3fs", len(atspi_result), elapsed
        )
        return screenshot_bytes, atspi_result, None, elapsed, "atspi", img_w, img_h

    # VLM fallback
    Debug.save(
        "input_screenshot",
        screenshot_bytes,
        ext="png",
        invocation_id=invocation_id,
    )
    elements, vlm_elapsed, raw_text, vlm_label = await _vlm_detect_elements(
        screenshot_bytes,
        context,
        img_w,
        img_h,
        crop_offset=crop,
        invocation_id=invocation_id,
        model_override=model_override,
    )
    if elements is None:
        detail = (
            "VLM detected 0 interactive elements — "
            "the model may not be seeing the UI correctly.\n"
            f"Raw VLM response:\n{raw_text}"
        )
        _log.info("detect_elements: vlm fallback 0 elements in %.3fs", vlm_elapsed)
        return (
            screenshot_bytes,
            [],
            detail,
            vlm_elapsed,
            f"vlm {vlm_label}",
            img_w,
            img_h,
        )
    elements = DesktopElement.filter_junk(elements, titlebar_y)
    # Single pass — one screenshot, one VLM call. The multi-pass refinement (dense strips +
    # quadrants) multiplied calls into 100s+ on large screens; recall is recovered on demand
    # instead, since a follow-up (query-focused) detect accumulates into the window's refs.
    elements = DesktopElement.merge_into(win.wid, elements, page_sig)
    _log.info(
        "detect_elements: vlm fallback %d elements in %.3fs", len(elements), vlm_elapsed
    )
    return (
        screenshot_bytes,
        elements,
        None,
        vlm_elapsed,
        f"vlm {vlm_label}",
        img_w,
        img_h,
    )


def _crop_image(png_bytes: bytes, x: int, y: int, w: int, h: int) -> bytes:
    """Crop a PNG image to (x, y, w, h) region."""
    img = PILImage.open(io.BytesIO(png_bytes))
    cropped = img.crop((x, y, x + w, y + h))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def _desktop_context(win: DesktopWindow) -> str:
    return f"Desktop window: {win.name} ({win.w}x{win.h})"
