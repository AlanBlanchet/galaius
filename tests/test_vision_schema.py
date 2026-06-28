"""Models that reject a native ``response_format`` (e.g. zai/GLM → litellm UnsupportedParamsError)
must still run the structured tools: interact asks for JSON in the PROMPT instead of erroring into a
frontier fallback. This is the bug behind "the sovereign tier silently falls back to gemini": review_ui
selected GLM, GLM rejected response_format, and the chain dropped to gemini — so GLM never ran."""

import json
from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel

import interact.vision as v


class _Tiny(BaseModel):
    color: str


def _resp(content: str):
    return NS(choices=[NS(finish_reason="stop", message=NS(content=content))], usage=NS(completion_tokens=7))


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    monkeypatch.setattr(v, "_log_usage", lambda *a, **k: None)


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
