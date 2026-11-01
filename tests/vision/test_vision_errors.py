"""A provider fault on the model call — no credits, a rejected key, an unknown model id, no answer —
reaches the agent in galaius's own words with the way out, never as a raw ``litellm.RateLimitError``
traceback (#124, #125). The translation lives at the ONE litellm seam (``_vision_completion`` and
``transcribe_audio``); ``@instrumented`` closes the ``ERROR:`` contract. The same contract is what
``srv._vlm``'s own model-chain fallback (breaker-gated primary → fallback) resolves to: a
successful fallback's result, or — every candidate exhausted — one ``ERROR:`` line carrying the
last failure's own words."""

import io
from unittest.mock import AsyncMock, patch

import litellm
import pytest
from PIL import Image as PILImage

import galaius.vision.core as v
from galaius.config.settings import Config
from galaius.vision.types import VLMResult
from galaius.vision.core import VisionError
from galaius.diagnostics.debug_output import Debug


@pytest.fixture
def srv(provider_key):
    import galaius.server as _srv
    from galaius.runtime import breaker

    breaker.clear()
    _srv.core.config.component_criteria = "cap.gui_grounding"
    with patch.object(Debug, "save"):
        yield _srv
    _srv.core.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()


# A frame with CONTENT in it, not a flat fill: an empty frame is now short-circuited before any
# model call (#112), so a 1x1 red square would make every fallback test below assert on a chain
# that never ran.
_img = PILImage.new("RGB", (8, 8))
for _x in range(8):
    for _y in range(8):
        _img.putpixel((_x, _y), (_x * 32, _y * 32, 0))
_buf = io.BytesIO()
_img.save(_buf, format="PNG")
_PNG = _buf.getvalue()

_MSGS = [{"role": "user", "content": [{"type": "text", "text": "what is this"}]}]
# The shape litellm really hands back for #124: its own stacked prefixes, the provider's sentence,
# then a body the agent cannot act on. Only the provider's sentence should survive into the message.
_SAID = "You have no credits remaining. Add credits to continue using the API"
_RAW = f"RateLimitError: OpenAIException - {_SAID}.\n{{'error': {{'code': 'insufficient_quota'}}}}"


def _raising(exc):
    async def fake(**_kwargs):
        raise exc

    return fake


@pytest.mark.parametrize(
    "exc, cause",
    [
        (
            litellm.exceptions.RateLimitError(message=_RAW, llm_provider="openai", model="gpt-x"),
            "rate-limited or out of credits",
        ),
        (
            litellm.exceptions.AuthenticationError(message=_RAW, llm_provider="openai", model="gpt-x"),
            "rejected the API key",
        ),
        (
            litellm.exceptions.NotFoundError(message=_RAW, model="gpt-x", llm_provider="openai"),
            "does not know this model id",
        ),
        (litellm.exceptions.Timeout(message=_RAW, model="gpt-x", llm_provider="openai"), "did not answer"),
        (
            litellm.exceptions.APIConnectionError(message=_RAW, llm_provider="openai", model="gpt-x"),
            "did not answer",
        ),
        # Any other litellm provider error (a 5xx here) gets the generic cause + the same way out.
        (
            litellm.exceptions.InternalServerError(message=_RAW, llm_provider="openai", model="gpt-x"),
            "failed the request",
        ),
    ],
    ids=[
        "RateLimitError",
        "AuthenticationError",
        "NotFoundError",
        "Timeout",
        "APIConnectionError",
        "InternalServerError",
    ],
)
@pytest.mark.asyncio
async def test_a_provider_fault_becomes_a_vision_error_saying_the_way_out(monkeypatch, exc, cause):
    monkeypatch.setattr(v.litellm, "acompletion", _raising(exc))
    with pytest.raises(VisionError) as info:
        await v._vision_completion(_MSGS, "openai/gpt-x")
    text = str(info.value)
    assert "openai/gpt-x" in text, text  # the id the caller passed, not litellm's stripped one
    assert cause in text, text  # the one-line human cause
    assert _SAID in text and "insufficient_quota" not in text, text  # the provider's sentence, not its body
    assert "model=" in text and "image.criteria" in text and "list_providers" in text, text
    assert info.value.__cause__ is exc  # the raw class stays reachable for logs


@pytest.mark.asyncio
async def test_a_non_provider_exception_is_not_dressed_up(monkeypatch):
    """Only litellm's provider faults are translated: a bug in galaius's own code stays a bug."""
    monkeypatch.setattr(v.litellm, "acompletion", _raising(ValueError("galaius bug")))
    with pytest.raises(ValueError):
        await v._vision_completion(_MSGS, "openai/gpt-x")


@pytest.mark.asyncio
async def test_transcription_shares_the_translation(monkeypatch, media_output_root):
    """``transcribe_audio`` is the OTHER litellm call; the same 429 must read the same way there."""
    monkeypatch.setattr(v, "_key_missing", lambda model: None)
    monkeypatch.setattr(
        v.litellm,
        "atranscription",
        _raising(litellm.exceptions.RateLimitError(message=_RAW, llm_provider="openai", model="whisper-1")),
    )
    with pytest.raises(VisionError) as info:
        await v.transcribe_audio(
            b"RIFF....WAVE", model="whisper-1", mime_type="audio/wav",
            config=Config(debug_dir=media_output_root, media_backend="api", media_billing="api_allowed"),
        )
    text = str(info.value)
    assert "whisper-1" in text and "out of credits" in text and _SAID in text, text


# =============================================================================================
# _vlm model-chain fallback + circuit breaker (rate limit / any error)
# =============================================================================================


@pytest.mark.asyncio
async def test_vlm_rate_limit_triggers_fallback(srv):
    """RateLimitError in _vlm trips breaker and tries fallback model."""
    from litellm.exceptions import RateLimitError
    from galaius.config.settings import Config
    from galaius.models.registry import Model
    from galaius.models.capability import ModelCapability
    from galaius.runtime import breaker

    call_count = 0

    async def _mock_analyze(
        media, context, cfg, query, max_tokens, response_format, model, _dispatch_state,
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RateLimitError("rate limited", "test", "test")
        return VLMResult(text="fallback response", elapsed=0.1, model="fallback/model")

    # Two-model chain: the primary now resolves at the boundary (resolve_model → first available
    # = primary/model), fails, and the breaker trips IT — then the chain advances to fallback.
    primary = Model(
        id="primary/model", provider="test", capabilities={ModelCapability.VLM}
    )
    fallback = Model(
        id="fallback/model", provider="test", capabilities={ModelCapability.VLM}
    )
    with (
        patch("galaius.vision.core._api_media_completion", _mock_analyze),
        patch.object(Config, "ranked_models", return_value=[primary, fallback]),
        patch.object(Model, "is_available", return_value=True),
    ):
        result = await srv.vlm._vlm(_PNG, "test context", "test query")

    assert call_count == 2
    assert breaker.tripped("primary/model")
    assert "fallback" in result.text.lower()


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: __import__("litellm").exceptions.RateLimitError(
            "rate limited", "test", "test"
        ),
        lambda: __import__("litellm").exceptions.APIError(500, "boom", "test", "test"),
        lambda: ValueError("bad payload"),
        lambda: VisionError(
            "primary/model", provider="openai", cause="is rate-limited or out of credits", said="no credits"
        ),
    ],
    ids=["RateLimitError", "APIError", "ValueError", "VisionError"],
)
@pytest.mark.asyncio
async def test_vlm_falls_back_on_error(srv, make_error):
    """Any non-cancellation exception triggers fallback chain (not only RateLimitError)."""
    from galaius.config.settings import Config
    from galaius.models.registry import Model
    from galaius.models.capability import ModelCapability

    call_count = 0

    async def _mock(
        media, context, cfg, query, max_tokens, response_format, model, _dispatch_state,
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise make_error()
        return VLMResult(text="recovered", elapsed=0.1, model="fallback/model")

    primary = Model(
        id="primary/model", provider="test", capabilities={ModelCapability.VLM}
    )
    fallback = Model(
        id="fallback/model", provider="test", capabilities={ModelCapability.VLM}
    )
    with (
        patch("galaius.vision.core._api_media_completion", _mock),
        patch.object(Config, "ranked_models", return_value=[primary, fallback]),
        patch.object(Model, "is_available", return_value=True),
    ):
        result = await srv.vlm._vlm(_PNG, "ctx")

    assert call_count == 2
    assert result.text.startswith("[Fallback: used fallback/model")
    assert "recovered" in result.text


@pytest.mark.asyncio
async def test_a_provider_fault_on_a_page_query_reaches_the_agent_as_an_ERROR_string(srv, monkeypatch):
    """`screenshot(query=…)` on a browser page surfaced OpenAI's no-credits 429 as a raw
    ``litellm.RateLimitError`` (#124, #125): the page-query path calls the model with no fallback
    chain around it, so the exception left the tool instead of taking the documented ``ERROR:``
    shape every other failure has. Closed at the same seam as CaptureError — ``@instrumented`` — so
    navigate(query=) and the next page-query tool get it for free."""
    import base64
    from unittest.mock import MagicMock

    from litellm.exceptions import RateLimitError

    state = MagicMock(title="Billing", url="http://x", screenshot_base64=base64.b64encode(_PNG).decode())

    async def no_credits(**_kwargs):
        raise RateLimitError(
            message="RateLimitError: OpenAIException - You have no credits remaining.",
            llm_provider="openai",
            model="gpt-4o",
        )

    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (None, MagicMock(), None))
    monkeypatch.setattr(srv.capture, "_capture", AsyncMock(return_value=state))
    monkeypatch.setattr("galaius.vision.core.litellm.acompletion", no_credits)
    monkeypatch.setattr("galaius.vision.core._key_missing", lambda model: None)
    fn = getattr(srv.tools.vision.screenshot, "fn", srv.tools.vision.screenshot)
    out = await fn(query="what is on this page?")
    assert isinstance(out, str), "a provider fault must be a readable result, not an exception"
    assert out.startswith("ERROR:"), out
    assert "no credits remaining" in out and "out of credits" in out, out
    assert "list_providers" in out and "image.criteria" in out, out


@pytest.mark.parametrize(
    "make_error, expect",
    [
        (
            lambda m: VisionError(m, provider="openai", cause="is rate-limited or out of credits", said="no credits"),
            "list_providers",
        ),
        (lambda m: ValueError("bad payload"), "ValueError: bad payload"),
    ],
    ids=["VisionError", "ValueError"],
)
@pytest.mark.asyncio
async def test_vlm_exhausted_chain_is_an_ERROR_line_that_says_why(srv, make_error, expect):
    """Every model failed → the agent used to get "[All 2 fallbacks failed — last error on X:
    RateLimitError]": a class name, no ERROR: prefix, no way out. Now it is an ERROR: line carrying
    the last failure's own words — a VisionError's guidance, or the class + message of anything else."""
    from galaius.config.settings import Config
    from galaius.models.registry import Model
    from galaius.models.capability import ModelCapability

    async def _mock(
        media, context, cfg, query, max_tokens, response_format, model, _dispatch_state,
    ):
        raise make_error(model)

    primary = Model(id="primary/model", provider="test", capabilities={ModelCapability.VLM})
    fallback = Model(id="fallback/model", provider="test", capabilities={ModelCapability.VLM})
    with (
        patch("galaius.vision.core._api_media_completion", _mock),
        patch.object(Config, "ranked_models", return_value=[primary, fallback]),
        patch.object(Model, "is_available", return_value=True),
    ):
        result = await srv.vlm._vlm(_PNG, "ctx")

    assert result.text.startswith("ERROR:"), result.text
    assert "fallback/model" in result.text and expect in result.text, result.text


@pytest.mark.asyncio
async def test_a_missing_key_raises_so_the_fallback_chain_walks_on(monkeypatch):
    """A placeholder answer used to stop the chain at its first candidate, and review_ui then parsed
    "[Vision unavailable …" as its JSON (`Expecting value: line 1 column 2`)."""
    from galaius.vision.types import MediaItem

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    media = [MediaItem.from_bytes(_PNG)]
    with pytest.raises(VisionError, match="no API key configured"):
        await v._api_media_completion(media, "ctx", Config(), None, v.UNSET, None, "gemini/gemini-2.5-flash")


@pytest.mark.asyncio
async def test_a_missing_transcription_key_raises_never_a_transcript(monkeypatch):
    """The transcript is answered as speech downstream: a placeholder there reads as words said."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = Config(media_backend="api", media_billing="api_allowed")
    with pytest.raises(VisionError, match="no API key configured"):
        await v.transcribe_audio(b"RIFF\x08\x00\x00\x00WAVE", model="whisper-1", mime_type="audio/wav", config=config)
