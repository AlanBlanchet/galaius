"""The VLM boundary: one resolution+fallback path (``_vlm``), the observe/compare/media-response
wrappers around it, and the page/interaction analysis helpers. Everything that turns captured
bytes into a model verdict lives here, apart from the tools that call it."""

import asyncio
import logging
from dataclasses import dataclass

from interact.server import core
from interact.server.core import config
from interact.vision.core import (
    _UNSET,
    VisionError,
    _Unset,
    analyze_media,
    analyze_screenshot,
)
from interact.vision.measure import blank_frame_reason
from interact.vision.types import MediaItem, VLMResult, evenly_sampled

_log = logging.getLogger("interact")
def _describe(err: Exception) -> str:
    """The whole failure for the exhausted-chain ERROR: a VisionError already carries the way out;
    anything else needs its class name too, since str() of a bare exception can be empty."""
    return str(err) if isinstance(err, VisionError) else f"{type(err).__name__}: {err}".rstrip(": ")


def _effective_model(model_override: str | None, role: str) -> str:
    """The model id that will actually run for a role — delegates to the one resolution site
    (:meth:`Config.resolve_model`) so the resolved dump matches what the VLM path runs."""
    if config.media_sessions_enabled():
        return model_override or "subscription session (provider default)"
    return config.resolve_model(role, model_override or "")


def _resolved_config(model_override: str | None, role: str) -> dict:
    """The full effective config for tool_input_resolved.json, with the per-call effective model
    surfaced so the resolved dump reflects what actually ran (an override or the auto default) —
    not just the empty configured field."""
    resolved = config.model_dump(mode="json")
    try:
        resolved["effective_model"] = _effective_model(model_override, role)
    except RuntimeError as error:
        # Written before the call: the call itself reports an unresolvable model.
        resolved["effective_model"] = None
        resolved["effective_model_error"] = str(error)
    resolved["effective_model_role"] = role
    return resolved


async def _vlm(
    data: bytes,
    context: str,
    query: str | None = None,
    media_type: str = "image",
    mime: str | None = None,
    max_tokens: int | None | _Unset = _UNSET,
    response_format: type | dict | None = None,
    model_override: str | None = None,
    extra_images: list[MediaItem | bytes] | None = None,
    _api_model_override: str | None = None,
) -> VLMResult:
    # Empty frame decided on pixels, not by a model. Asked to describe a black capture of a
    # crashed window, the VLM once answered anyway — returning the agent's own action text in a
    # full-frame bounding box, as if it had read it on screen (#112).
    #
    # Here, not in _media_response: that's one caller of three — review_ui/verify_ui reach the
    # model through this function directly, describing a frame is their whole job, so they were
    # most exposed to the bug.
    if media_type == "image" and (why := blank_frame_reason(data)):
        return VLMResult(
            text=(
                f"ERROR: nothing to analyse — {why}. Not sent to the model. The window may be "
                "crashed, unmapped, or on a GPU surface the grabber cannot read; re-check with "
                "return_image=True, or capture target=\"screen\"."
            ),
            elapsed=0.0,
            model="(not called)",
        )
    item_type = media_type if media_type in ("video", "audio") else "image"
    routing = media_type or "image"
    # extra_images ride alongside the primary frame in ONE call (e.g. reference + build, for a
    # divergence review) — judging two images together stops isolation-against-a-generic-ideal
    # false PASSes seen in real usage.
    media = [MediaItem.from_bytes(data, item_type, mime)]
    media += [
        item if isinstance(item, MediaItem) else MediaItem.from_bytes(item)
        for item in (extra_images or [])
    ]
    dispatch_state: list[bool] = []
    try:
        result = await analyze_media(
            media,
            context,
            config,
            query,
            max_tokens=max_tokens,
            response_format=response_format,
            model=model_override or "",
            role=routing,
            _api_model=_api_model_override or "",
            _dispatch_state=dispatch_state,
        )
        if result.dispatch_status != "not_requested":
            return result
        return result.model_copy(update={
            "dispatch_eligible": True,
            "dispatch_attempted": True,
            "dispatch_status": "completed",
        })
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        attempted = bool(dispatch_state)
        return VLMResult(
            text=f"ERROR: media analysis failed — {_describe(exc)}",
            elapsed=0,
            model=model_override or "",
            dispatch_eligible=attempted,
            dispatch_attempted=attempted,
            dispatch_status="failed" if attempted else "unavailable",
        )


def _fmt_timing(r: VLMResult) -> str:
    identity = r.model or (
        f"{r.provider} subscription session (provider default)"
        if r.backend == "session" and r.provider else ""
    )
    model_tag = f" {identity}" if identity else ""
    return f"{r.text}\n(VLM:{model_tag} {r.elapsed:.1f}s)"


async def _run_observe(screenshot_bytes: bytes, query: str, context: str) -> str:
    try:
        r = await _vlm(screenshot_bytes, context, query)
        if r.text.startswith("ERROR:"):
            return f"observe error: {r.text.removeprefix('ERROR:').strip()}"
        return _fmt_timing(r)
    except Exception as e:
        return f"observe error: {e}"


async def _run_compare(
    snapshots: dict[int, bytes], steps: list[int], query: str, context: str
) -> str:
    missing = [s for s in steps if s not in snapshots]
    if missing:
        return ", ".join(
            f"Step {s} has no snapshot — add observe to that action" for s in missing
        )
    try:
        media = [MediaItem.from_bytes(snapshots[s]) for s in steps]
        r = await analyze_media(media, context, config, query, role="image")
        return _fmt_timing(r)
    except Exception as e:
        return f"compare error: {e}"


#: Every query answer is a REPORT from the agent's eyes, never an act — without this, a query
#: ("close the dialog") read as an instruction and came back as "Closed." (#119).
_OBSERVER = (
    "You are the eyes of an automation agent and can only LOOK. Answer with what is visible — "
    "which element (by its number), where it is, what state it is in — never as an action you "
    "took ('Clicked X', 'Closed'): the agent decides what to do with what you saw."
)


@dataclass(frozen=True)
class _MediaResponse:
    text: str | None
    result: VLMResult | None


async def _media_response(
    data: bytes,
    context: str,
    query: str | None = None,
    path: str | None = None,
    media_type: str = "image",
    mime: str = "image/png",
    model_override: str | None = None,
) -> _MediaResponse:
    """Analyse ``data`` when a ``query`` is given; save it when a ``path`` is given. Returns the
    analysis, or None when there was neither. Whenever a file was written the reply ends with where
    it landed (``context`` standing in for the analysis when no query was asked), so no tool that
    routes its save through here can stay silent about the location (#120)."""
    result = None
    vlm_result: VLMResult | None = None
    try:
        if query:
            vlm_result = await _vlm(
                data, f"{_OBSERVER}\n\n{context}", query, media_type, mime, model_override=model_override
            )
            result = _fmt_timing(vlm_result)
    finally:
        # Save AFTER the (slow) VLM call, in a finally — file on disk is exactly the frame
        # analyzed/returned, still written even if the VLM errors (#17).
        dest = core._save_to_path(path, data) if path else None
    response = result if dest is None else f"{result or context}\n{core._saved_note(dest, data)}"
    return _MediaResponse(response, vlm_result)


async def _analyze(state, query: str | None = None, model_override: str | None = None) -> str:
    if model_override:
        media = [MediaItem(data=state.screenshot_base64)]
        r = await analyze_media(
            media,
            f"Page: {state.title} ({state.url})",
            config,
            query,
            model=model_override,
            role="image",
        )
    else:
        r = await analyze_screenshot(state, config, query)
    return _fmt_timing(r)


async def _analyze_interaction_frames(frames: list[bytes], query: str | None) -> str:
    """Analyse the per-step frames of an interaction as an ordered sequence, so a model sees what
    each action produced — not just the end state. One frame per step is captured during the run;
    here it's sampled down to config.video_max_frames (evenly) to bound cost, then sent to the
    video model with the query."""
    sampled = evenly_sampled(frames, config.video_max_frames)
    media = [MediaItem.from_bytes(f, "image", "image/png") for f in sampled]
    context = (
        f"{len(sampled)} screenshots captured in order during an interaction — each is the page/"
        "window state right after one step. Read them as a sequence to see what happened."
    )
    r = await analyze_media(
        media,
        context,
        config,
        query or "Describe what happened across these frames, step by step.",
        role="video",
    )
    return f"\n\n[recording: {len(sampled)} frames] {_fmt_timing(r)}"
