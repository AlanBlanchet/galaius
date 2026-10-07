"""Vision + capture MCP tools: screenshot, get_interactive_elements, review_ui, verify_ui,
measure_ui, transcribe. They resolve the target/quality plan and delegate to the capture + vlm
helpers; the discover→verify→measure trio share the ``_run_ui_critique`` runner."""

import base64
import logging
import re
from pathlib import Path

from mcp.server.fastmcp.utilities.types import Image

from galaius.config import DEFAULT_LIMIT
from galaius.debug_utils import Debug
from galaius.desktop import DesktopElement
from galaius.models import is_audio_model, is_transcription_only_model
from galaius.server import capture, core, targets, vlm
from galaius.server.core import (
    _AUTO_SESSION,
    _audio_mime,
    _session_response,
    config,
    instrumented,
    mcp,
)
from galaius.state import format_element_list
from galaius.vision import MediaItem
from galaius.vision.core import analyze_media, transcribe_audio
from galaius.vision.critique import (
    UIReview,
    VerifyReport,
    build_review_prompt,
    build_verify_prompt,
    format_review,
    format_verify,
    parse_review,
    parse_verify,
)
from galaius.vision.detect import _crop_image, _desktop_context, _page_signature
from galaius.vision.measure import blank_frame_reason, format_measure, measure

_log = logging.getLogger("galaius")


@mcp.tool(category="vision")
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
    session: str = _AUTO_SESSION,
    model: str | None = None,
):
    """Capture the current page or a desktop window.

    Default (target unset): operates on your own browser session (named in the reply).
    target=<window title>: captures a desktop window. target="screen"/"screen:<index>": the whole
    desktop or one monitor (use list_desktop_windows to discover windows + monitor indexes).
    target="file:<path>": ANALYZE an existing image file (with query) instead of capturing — for an
    artifact produced out-of-band; this never writes, so it can't clobber the file.
    A desktop target and a session you named are mutually exclusive.

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
    path: OUTPUT sink — saves the captured PNG here (overwrites any existing file, and says so). A
        relative path lands under ~/.galaius/out (galaius's output dir), never the server's cwd;
        "~" expands. The reply names the absolute file written. To ANALYZE an existing image, use
        target="file:<path>", not path.
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
        media_mime = MediaItem.detect_mime(file_bytes)
        media_format = media_mime.split("/", 1)[1]
        if query:
            r = await vlm._vlm(
                file_bytes, label, query, mime=media_mime, model_override=model
            )
            text = f"{label}\n{vlm._fmt_timing(r)}"
        else:
            import io as _io

            from PIL import Image as _PILImage
            w, h = _PILImage.open(_io.BytesIO(file_bytes)).size
            text = f"{label} ({w}x{h}) — pass query=… to analyze it, or use measure_ui for exact pixels."
        Debug.save("capture", file_bytes, ext=media_format, invocation_id=inv)
        out = [text, Image(data=file_bytes, format=media_format)] if return_image else text
        return out
    win, mgr, err = targets._resolve_target(target, session)
    if err:
        return err
    # If `path` already exists we're about to OVERWRITE it with this capture — surface that so the
    # result can't be mistaken for an analysis of the prior file (#44). To analyze a file, use
    # target="file:<path>" above — `path` is an OUTPUT sink.
    overwrote_path = bool(path) and core._resolve_save_path(path).exists()
    img_bytes: bytes | None = None
    if win:
        if element is not None:
            raw = win.capture()
            el = targets._resolve_desktop_el(win.wid, win.name, element=element)
            if el is None:
                nf = core._not_found(f"Element {element}")
                return nf
            img_bytes = _crop_image(raw, el.x, el.y, el.w, el.h)
            geometry = f"({el.w}x{el.h} at {el.x},{el.y})"
            # The LABEL is what must not survive a screen change. Cropping the live frame at a
            # ref's coordinates is what was asked for; telling the model those pixels are a widget
            # detected on a DIFFERENT frame hands it an image and a description that disagree — the
            # setup for the confident wrong answer in #112. So on a changed frame the crop still
            # goes, described only by where it was taken from, and the caller is warned.
            #
            # A warning, not a refusal, deliberately: the signature is a 16x16 hash of the frame,
            # so a blinking caret or a clock flips it, and refusing there would make element
            # queries unusable on any live window — matches how this codebase already treats the
            # geometry case (DesktopElement.detection_stale).
            stale = DesktopElement.stale_for(win.wid, _page_signature(raw))
            meta = geometry if stale else f"[{el.index}] {el.role}: {el.name!r} {geometry}"
            result = await vlm._media_response(img_bytes, meta, query, path, model_override=model)
            note = (
                f"\n(note: ref [{el.index}] was detected on a different frame — describing the "
                f"region at {geometry} without its recorded label; re-run "
                "get_interactive_elements if this is not the element you meant)"
                if stale else ""
            )
            text = f"{core._desktop_label(win)}\n{result.text or meta}{note}"
        elif query:
            img_bytes, description = await capture._capture_desktop(win, query, path, model_override=model)
            text = f"{core._desktop_label(win)}\n{description}"
        else:
            # No query → just capture. screenshot NEVER runs VLM grounding (get_interactive_elements'
            # job; a VLM call here would be slow + wrong). If a detection already exists for this
            # window, surface those refs so the capture is actionable; otherwise return metadata
            # and point the agent at the detect tool.
            img_bytes = win.capture()
            dest = core._save_to_path(path, img_bytes) if path else None
            # Surface cached refs ONLY if they belong to the frame just captured — after a navigation
            # the live frame's signature differs, so we don't list a prior screen's refs on a screen
            # that's no longer shown (the screenshot↔elements desync, #19).
            cached = DesktopElement.cached_for(win.wid, _page_signature(img_bytes))
            if cached:
                text = f"{core._desktop_label(win)}\n{DesktopElement.format_list(cached)}"
            else:
                text = (
                    f"{core._desktop_label(win)}\n{_desktop_context(win)}\n"
                    "(call get_interactive_elements to detect clickable elements and act by [ref])"
                )
            if dest is not None:
                text += f"\n{core._saved_note(dest, img_bytes)}"
    elif element is not None or selector is not None:
        text = _session_response(
            session, await capture._element_screenshot(mgr, mgr.active_tab, selector, element, query, path)
        )
    else:
        state = await capture._capture(mgr, scope)
        img_bytes = base64.b64decode(state.screenshot_base64)
        dest = core._save_to_path(path, img_bytes) if path else None
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
        if dest is not None:
            text += f"\n{core._saved_note(dest, img_bytes)}"
    if overwrote_path:
        text += "\n(note: overwrote existing file with this capture)"
    # An empty frame is indistinguishable from "still loading", so a caller retries and waits
    # instead of looking — exactly what happened to a CRASHED window whose per-window capture
    # came back black while target="screen" showed the crash modal (#113). One line.
    if img_bytes is not None and "nothing to analyse" not in text and (why := blank_frame_reason(img_bytes)):
        text += (
            f"\n(note: {why} — the window may be crashed, occluded or GPU-composited"
            + (' ; try target="screen", which shows crash modals per-window capture misses)' if win else ")")
        )
    if img_bytes is not None:
        Debug.save("capture", img_bytes, ext="png", invocation_id=inv)
    result = [text, Image(data=img_bytes, format="png")] if (return_image and img_bytes is not None) else text
    return result


@mcp.tool(category="vision")
@instrumented
async def get_interactive_elements(
    scope: str | None = None,
    query: str | None = None,
    element: int | None = None,
    limit: int = DEFAULT_LIMIT,
    tab: int | None = None,
    debug_dir: str | None = None,
    target: str | None = None,
    session: str = _AUTO_SESSION,
    method: str = "default",
    model: str | None = None,
    fresh: bool = False,
) -> str:
    """List the interactive elements with numbered badges + their details; act on them by the
    returned `ref`/`element` in run_actions.

    Default (target unset): your own browser session — sets data-galaius-ref attributes via a
    pure DOM scan (no VLM). get_page_state and screenshot return these refs too, so you often
    already have them without a separate call. target=<window title>: VLM-detects elements in a
    desktop window;
    target="screen"/"screen:<index>": VLM-detects across the whole desktop or one monitor.
    A desktop target and a session you named are mutually exclusive (list_desktop_windows lists them).

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


@mcp.tool(category="vision")
async def review_ui(
    focus: str | None = None,
    reference: str | None = None,
    target: str | None = None,
    session: str = _AUTO_SESSION,
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
    path: save the reviewed PNG here. A relative path lands under ~/.galaius/out (galaius's output
        dir), never the server's cwd; "~" expands. Requires a configured vision model (same as
        screenshot's query).
    quality: pick the model by STAKES, not by name — "low"/"medium" use a cheap sovereign self-host
        model, "high"/"critical" the best frontier model; "critical" also drops findings whose element
        galaius can't confirm (highest precision, for a pre-ship sign-off). Unset = the configured/
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


@mcp.tool(category="vision")
async def verify_ui(
    requirements: list[str],
    target: str | None = None,
    reference: str | None = None,
    focus: str | None = None,
    session: str = _AUTO_SESSION,
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
    path: save the judged PNG here. A relative path lands under ~/.galaius/out (galaius's output
        dir), never the server's cwd; "~" expands.
    quality: pick the model by STAKES — "low"/"medium" use a cheap sovereign model, "high"/"critical"
        the best frontier; "critical" downgrades any PASS resting on an element galaius can't confirm.
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


@mcp.tool(category="vision")
@instrumented
async def measure_ui(
    target: str | None = None,
    region: str | None = None,
    point: str | None = None,
    session: str = _AUTO_SESSION,
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
    path: save the measured PNG here. A relative path lands under ~/.galaius/out (galaius's output
        dir), never the server's cwd; "~" expands. The reply names the absolute file written.
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
    dest = core._save_to_path(path, img) if path else None
    try:
        result = measure(img, region=reg, point=pt)
    except Exception as e:
        return f"ERROR: measure_ui failed — {e}"
    out = f"{label}\n{format_measure(result)}"
    if dest is not None:
        out += f"\n{core._saved_note(dest, img)}"
    return out


_SOUND_QUALITY_RE = re.compile(
    r"\b(fidelity|artifacts?|artefacts?|distortion|distorted|noisy|noise|clean(?:er|est)?|"
    r"crisp|muffled|robotic|metallic|watery|phasey|tinny|clipping|quality|prosody|"
    r"sound(?:s|ing)? (?:good|bad|better|worse)|which .{0,20}\bbetter\b)",
    re.IGNORECASE,
)


def _asks_about_sound_quality(query: str | None) -> bool:
    """Is this query a judgement about how the audio SOUNDS (fidelity, artifacts, which is
    cleaner) rather than what it CONTAINS (words, speakers, language, timing)?

    Such answers proved unreliable: on one clean file the same model once rated fidelity, then
    claimed it had only received a transcript, then inverted an A/B comparison against an
    objectively measured relationship (#94). The verdict is a caption, never a measurement — so
    these queries get told so."""
    return bool(query) and bool(_SOUND_QUALITY_RE.search(query))


_QUALITY_CAVEAT = (
    "\n\nNOTE: this is a model's IMPRESSION of the sound, not a measurement. Such verdicts shift "
    "with the wording of the question and have been observed to invert A/B comparisons against an "
    "objectively measured relationship (#94). For a quality claim you intend to rely on, use an "
    "objective metric against the real target (PESQ / STOI / ASR-WER vs a reference), and treat "
    "this text as corroboration only."
)


@mcp.tool(category="media")
@instrumented
async def transcribe(path: str, query: str | None = None, model: str | None = None) -> str:
    """Transcribe an audio (or audio-bearing) file to text, and optionally answer a question about it.

    Point it at a local file `path` — a clip you grabbed with download_asset, or a recording you
    saved with record(path=...). Accepts mp3/wav/m4a/webm/ogg/flac and mp4/mov (the audio track is
    used). Returns the transcript; with `query`, returns an answer about the audio instead.

    Audio understanding is acoustic (it HEARS the clip — tone, speakers, music, sound events) when the
    audio model can take audio in chat (Gemini, gpt-4o-audio); with a transcription-only model
    (Whisper, gpt-4o-transcribe) the query is answered over the transcript. Set the model with the
    `audio.model` setting / GALAIUS_AUDIO_MODEL, or override per-call with `model`. Claude
    subscription sessions are visual-only: audio requires `media.billing=api_allowed` and uses the
    configured API or local-compatible audio transport regardless of `media.backend`; transcript
    Q&A may use a session after a transcription-only model has produced text.

    path: local audio/media file to read.
    query: optional question about the audio (omit for a plain transcript).
    model: override the configured audio model for this call.
    """
    config.refresh()
    if config.media_billing != "api_allowed":
        return (
            "ERROR: subscription sessions cannot transcribe audio. Configure audio.model for an "
            "API or local-compatible transcription backend and set media.billing=api_allowed; "
            "transcript Q&A can use a Claude subscription session after that transcription step."
        )
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
            out = f"(heard the audio: {audio_model})\n{vlm._fmt_timing(r)}"
            return out + _QUALITY_CAVEAT if _asks_about_sound_quality(query) else out
        except Exception as e:
            return f"ERROR: audio understanding failed on {audio_model} — {e}"

    try:
        r = await transcribe_audio(data, model=audio_model, mime_type=mime, config=config)
    except Exception as e:
        return f"ERROR: transcription failed on {audio_model} — {e}"
    transcript = r.text
    if not query:
        return f"{transcript}\n(transcribed:{(' ' + r.model) if r.model else ''} {r.elapsed:.1f}s)"

    # The model never heard the clip — it's reading the transcript. Say so: a confident
    # acoustic-sounding answer over text is exactly the failure reported in #94. A question about
    # how it SOUNDS can't be answered from a transcript at all, so refuse rather than invent one.
    if _asks_about_sound_quality(query):
        return (
            f"CANNOT ANSWER: {audio_model} is transcription-only, so this query was going to be "
            f"answered from the TRANSCRIPT — text carries no acoustic information, and an answer "
            f"from it would be invented. Configure an audio-capable model (Gemini, gpt-4o-audio) "
            f"to have the clip actually heard, and back any quality claim with an objective metric "
            f"(PESQ / STOI / ASR-WER vs a reference).\n\n--- transcript ---\n{transcript}"
        )
    answer = await analyze_media(
        [], f"Transcript of {name}:\n{transcript}", config, query, role="image"
    )
    return (
        f"(answered from the TRANSCRIPT — {audio_model} is transcription-only and did not hear "
        f"the audio)\n{answer.text}\n\n--- transcript ---\n{transcript}"
        f"\n(VLM: {answer.model} {answer.elapsed:.1f}s)"
    )
