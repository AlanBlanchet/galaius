"""What a vision-model response carries back across the seam: the schema request and the framing.

Models that reject a native ``response_format`` (e.g. zai/GLM → litellm UnsupportedParamsError)
must still run the structured tools: interact asks for JSON in the PROMPT instead of erroring into
a frontier fallback. This is the bug behind "the sovereign tier silently falls back to gemini":
review_ui selected GLM, GLM rejected response_format, and the chain dropped to gemini — so GLM
never ran.

A query answer is a REPORT from the agent's eyes, never an act (#119). The caller's query went to
the model verbatim, with nothing but "Annotated page with N elements" around it, so "close the
dialog" read as an instruction and came back as "Closed." — the agent then believed something had
happened. Every query answer crosses one seam; the framing lives there.

The request side of that same seam: `max_tokens=_UNSET` (the default) resolves to `config.max_tokens`
rather than litellm's own default, and an explicit value still overrides it.
"""

import io
import json
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
from PIL import Image as PILImage
from pydantic import BaseModel

import interact.server as srv
import interact.vision.core as v
from interact.vision.core import VLMResult

# A frame with CONTENT in it, not a flat fill (#112 short-circuits an empty frame before any
# model call, which would make this assert on a chain that never ran).
_img = PILImage.new("RGB", (8, 8))
for _x in range(8):
    for _y in range(8):
        _img.putpixel((_x, _y), (_x * 32, _y * 32, 0))
_buf = io.BytesIO()
_img.save(_buf, format="PNG")
_PNG = _buf.getvalue()


class _Tiny(BaseModel):
    color: str


def _resp(content: str):
    return NS(choices=[NS(finish_reason="stop", message=NS(content=content))], usage=NS(completion_tokens=7))


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    monkeypatch.setattr(v, "log_api_attempt", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_unschema_model_asks_for_json_in_the_prompt_not_response_format(monkeypatch):
    monkeypatch.setattr(v.litellm, "supports_response_schema", lambda model=None, **k: False)
    seen: dict = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _resp('{"color": "blue"}')

    monkeypatch.setattr(v.litellm, "acompletion", fake)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "what color"}]}]
    res = await v._vision_completion(msgs, "zai/glm-4.5v", response_format=_Tiny)

    assert "response_format" not in seen          # the param that raised UnsupportedParamsError is gone
    blob = json.dumps(seen["messages"])
    assert "color" in blob and "json" in blob.lower()   # the schema is requested via the prompt
    assert res.text == '{"color": "blue"}'        # the model's JSON flows back for the caller to parse


@pytest.mark.asyncio
async def test_schema_capable_model_keeps_the_native_response_format(monkeypatch):
    monkeypatch.setattr(v.litellm, "supports_response_schema", lambda model=None, **k: True)
    seen: dict = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _resp('{"color":"blue"}')

    monkeypatch.setattr(v.litellm, "acompletion", fake)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    res = await v._vision_completion(msgs, "gemini/g", response_format=_Tiny)

    assert seen["response_format"] is _Tiny        # native structured-output path unchanged for capable models
    assert res.text == '{"color":"blue"}'


@pytest.mark.asyncio
async def test_the_model_is_told_it_can_only_look(monkeypatch):
    seen: dict = {}

    async def capture(data, context, query, *a, **k):
        seen.update(context=context, query=query)
        return VLMResult(text="Element [2] is the dialog's close button, top right, enabled.", model="fake", elapsed=0.1)

    monkeypatch.setattr(srv.vlm, "_vlm", capture)
    out = await srv.vlm._media_response(b"png", "Annotated page with 2 elements:\n[1] OK\n[2] Close", query="close the dialog")
    assert out.text and "close button" in out.text
    assert "only LOOK" in seen["context"] and "never as an action you took" in seen["context"]
    assert "[2] Close" in seen["context"], "the element list must still reach the model"
    assert seen["query"] == "close the dialog", "the caller's words are not rewritten, only framed"


@pytest.mark.asyncio
async def test_unset_sentinel_uses_config_max_tokens():
    """When max_tokens=_UNSET (default), analyze_media uses config.max_tokens."""
    from unittest.mock import AsyncMock
    from interact.config import Config
    from interact.vision import MediaItem
    from interact.vision.core import _UNSET, analyze_media

    cfg = Config()
    media_item = [MediaItem.from_bytes(_PNG)]
    mock_completion = AsyncMock(return_value=VLMResult(text="ok", elapsed=0.1))
    # model is now resolved at the boundary and passed in; analyze_media no longer reads
    # config.model_for. It still validates the key, so patch that True.
    with (
        patch("interact.vision.core._vision_completion", mock_completion),
        patch(
            "interact.vision.core.litellm.validate_environment",
            return_value={"keys_in_environment": True},
        ),
    ):
        await analyze_media(media_item, "ctx", cfg, max_tokens=_UNSET, model="test-model")

    # Should have passed config.max_tokens (default: None)
    assert mock_completion.call_args.kwargs["max_tokens"] == cfg.max_tokens

    # Now pass explicit value -- should override
    with (
        patch("interact.vision.core._vision_completion", mock_completion),
        patch(
            "interact.vision.core.litellm.validate_environment",
            return_value={"keys_in_environment": True},
        ),
    ):
        await analyze_media(media_item, "ctx", cfg, max_tokens=512, model="test-model")

    assert mock_completion.call_args.kwargs["max_tokens"] == 512
