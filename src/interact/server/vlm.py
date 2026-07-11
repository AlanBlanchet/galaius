"""The VLM boundary: one resolution+fallback path (``_vlm``), the observe/compare/media-response
wrappers around it, and the page/interaction analysis helpers. Everything that turns captured
bytes into a model verdict lives here, apart from the tools that call it."""

import asyncio
import logging

from interact.server import core
from interact.server.core import breaker, config
from interact.vision import (
    _UNSET,
    MediaItem,
    VLMResult,
    _Unset,
    analyze_media,
    analyze_screenshot,
)

_log = logging.getLogger("interact")
_MAX_FALLBACKS = core._MAX_FALLBACKS


def _effective_model(model_override: str | None, role: str) -> str:
    """The model id that will actually run for a role — delegates to the one resolution site
    (:meth:`Config.resolve_model`) so the resolved dump matches what the VLM path runs."""
    return config.resolve_model(role, model_override or "")


def _resolved_config(model_override: str | None, role: str) -> dict:
    """The full effective config for tool_input_resolved.json, with the per-call effective model
    surfaced so the resolved dump reflects what actually ran (an override or the auto default) —
    not just the empty configured field."""
    resolved = config.model_dump(mode="json")
    resolved["effective_model"] = _effective_model(model_override, role)
    resolved["effective_model_role"] = role
    return resolved


async def _vlm(
    data: bytes,
    context: str,
    query: str | None = None,
    media_type: str = "image",
    mime: str = "image/png",
    max_tokens: int | None | _Unset = _UNSET,
    response_format: type | dict | None = None,
    model_override: str | None = None,
    extra_images: list[bytes] | None = None,
) -> VLMResult:
    item_type = "video" if media_type == "video" else "image"
    routing = media_type or "image"
    # extra_images ride alongside the primary frame in ONE call (e.g. a reference + the build, for a
    # divergence review) — judging two images together is what stops the isolation-against-a-generic-
    # ideal false PASSes seen in real usage.
    media = [MediaItem.from_bytes(data, item_type, mime)]
    media += [MediaItem.from_bytes(b, "image", mime) for b in (extra_images or [])]
    # Resolve ONCE, at this boundary, to a concrete id — then both the primary call and the
    # fallback chain run against real models. The old code resolved only for the fallback path
    # and handed the raw (often None) override to the primary call, so auto-mode vision always
    # hit analyze_media's empty-model branch → "[Vision not configured]" (39 real failures).
    effective_model = config.resolve_model(routing, model_override or "", breaker)

    async def _call(model_id: str) -> VLMResult:
        return await analyze_media(
            media,
            context,
            config,
            query,
            max_tokens=max_tokens,
            response_format=response_format,
            model=model_id,
        )

    try:
        return await _call(effective_model)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as primary_err:
        primary_type = type(primary_err).__name__
        _log.warning("%s on %s, attempting fallback chain", primary_type, effective_model)
        breaker.trip(effective_model)

        chain = config.chain_for(routing)
        candidates = [
            m
            for m in chain.preferences
            if m.id != effective_model and not breaker.tripped(m.id) and m.is_available()
        ]

        prev_model = effective_model
        prev_err_type = primary_type
        last_err: Exception = primary_err
        for fallback in candidates[:_MAX_FALLBACKS]:
            try:
                result = await _call(fallback.litellm_id())
                result.text = (
                    f"[Fallback: used {fallback.id} after {prev_model} "
                    f"failed with {prev_err_type}]\n\n{result.text}"
                )
                result.model = fallback.id
                return result
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as err:
                breaker.trip(fallback.id)
                prev_model = fallback.id
                prev_err_type = type(err).__name__
                last_err = err

        return VLMResult(
            text=(
                f"[All {1 + len(candidates[:_MAX_FALLBACKS])} fallbacks failed "
                f"— last error on {prev_model}: {type(last_err).__name__}]"
            ),
            elapsed=0,
            model=effective_model,
        )


def _fmt_timing(r: VLMResult) -> str:
    model_tag = f" {r.model}" if r.model else ""
    return f"{r.text}\n(VLM:{model_tag} {r.elapsed:.1f}s)"


async def _run_observe(screenshot_bytes: bytes, query: str, context: str) -> str:
    try:
        r = await _vlm(screenshot_bytes, context, query)
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
        r = await analyze_media(media, context, config, query, model=config.resolve_model("image"))
        return _fmt_timing(r)
    except Exception as e:
        return f"compare error: {e}"


async def _media_response(
    data: bytes,
    context: str,
    query: str | None = None,
    path: str | None = None,
    media_type: str = "image",
    mime: str = "image/png",
    model_override: str | None = None,
) -> str | None:
    try:
        if not query:
            return None
        r = await _vlm(data, context, query, media_type, mime, model_override=model_override)
        return _fmt_timing(r)
    finally:
        # Save AFTER the (slow) VLM call, in a finally — so the file on disk is exactly the frame
        # that was analyzed/returned, and is still written even if the VLM errors (#17).
        if path:
            core._save_to_path(path, data)


async def _analyze(state, query: str | None = None, model_override: str | None = None) -> str:
    if model_override:
        media = [MediaItem(data=state.screenshot_base64)]
        r = await analyze_media(
            media, f"Page: {state.title} ({state.url})", config, query, model=model_override
        )
    else:
        r = await analyze_screenshot(state, config, query)
    return _fmt_timing(r)


async def _analyze_interaction_frames(frames: list[bytes], query: str | None) -> str:
    """Analyse the per-step frames of an interaction as an ordered sequence, so a model sees what
    each action produced — not just the end state. One frame per step is captured during the run;
    here it's sampled down to config.video_max_frames (evenly) to bound cost, then sent to the
    video model with the query."""
    from interact.vision import evenly_sampled

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
        model=config.resolve_model("video"),
    )
    return f"\n\n[recording: {len(sampled)} frames] {_fmt_timing(r)}"
