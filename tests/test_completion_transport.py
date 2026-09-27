"""The explicit-completion transport (VS Code panel's non-Codex routes) hits a real vendor API
directly through litellm, so a quota/rate-limit refusal arrives as a raised exception, never as a
subprocess's stdout text. Nothing recorded it into the shared cooldown memory
(`~/.interact/out/agents/quota-cooldowns.json`) before this: a refusal heard here was invisible to
every other launch, including `ConversationRoute.resolve`'s own cooldown check and a fresh
CLI-subprocess spawn choosing the very model that just refused a moment ago on this route.
"""

import litellm
import pytest

from interact.agents import quota
from interact.agents.completion_transport import _CompletionTransport


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    """Every test writes its own cooldown file, never the developer's (mirrors test_agent_quota.py)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    quota.forget()


def _raising(exc):
    async def fake(**_kwargs):
        raise exc

    return fake


@pytest.mark.asyncio
async def test_a_quota_refusal_from_the_real_vendor_call_is_remembered(monkeypatch):
    """Wording already covered by quota.REFUSAL's own table (test_agent_quota.py) — this test
    proves the WIRING (transport → quota memory), not the regex coverage."""
    exc = litellm.exceptions.RateLimitError(
        message="Quota exceeded. Check your plan and billing details.",
        llm_provider="openai", model="gpt-x",
    )
    monkeypatch.setattr(litellm, "acompletion", _raising(exc))
    transport = _CompletionTransport(provider="openai")

    events = await transport.complete(
        run_id="r1", turn_id="t1", model="openai/gpt-x",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert quota.blocked_until("openai", "openai/gpt-x") is not None
    # The event shown to the user stays the existing generic text — this is a silent side channel,
    # never a UI change: dumping a raw litellm exception body into the chat panel is its own
    # regression, not something this fix should introduce.
    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].text == "Explicit API request failed."


@pytest.mark.asyncio
async def test_a_non_quota_failure_is_not_recorded_as_a_refusal(monkeypatch):
    """An ordinary provider fault (bad key, malformed response, timeout) must not cool the model
    down — that would be as wrong as replaying a dead model: refusing a HEALTHY one because an
    unrelated fault happened to be caught by the same except clause."""
    exc = litellm.exceptions.AuthenticationError(
        message="Incorrect API key provided.", llm_provider="openai", model="gpt-x",
    )
    monkeypatch.setattr(litellm, "acompletion", _raising(exc))
    transport = _CompletionTransport(provider="openai")

    await transport.complete(
        run_id="r1", turn_id="t1", model="openai/gpt-x",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert quota.blocked_until("openai", "openai/gpt-x") is None
