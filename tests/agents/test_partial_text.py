"""A text block reaches the mirror while it is written (owner 2026-11-23: « laggy and slow … from claude code in vscode it feels fast »).

`--include-partial-messages` makes the CLI write each block's words as deltas: the mirror holds the block's text so far a few
times a second (`partial`), never every delta, never thinking, a sub-agent's words or a half-written bracket, and the block's
closing `text` event wears the stamp of its first words, so a reader keeps one row from first word to last."""

import json

import pytest

from galaius.agents import registry as reg
from galaius.agents.providers import PROVIDERS
from galaius.agents.transport.events import NO_REPLY, PartialBlocks, UsageLedger
from tests.support import register_run


def _delta(text: str, *, index: int = 0, kind: str = "text_delta", parent: str | None = None, message: str = "m1") -> dict:
    field = "thinking" if kind == "thinking_delta" else "text"
    return {"type": "stream_event", "session_id": "s", "parent_tool_use_id": parent, "api_message_id": message,
            "event": {"type": "content_block_delta", "index": index, "delta": {"type": kind, field: text}}}


def _closing(text: str, message: str = "m1") -> dict:
    return {"type": "assistant", "session_id": "s", "message": {"id": message, "role": "assistant", "content": [{"type": "text", "text": text}]}}


def _words(text: str, size: int = 25, **fields) -> list[dict]:
    return [_delta(text[start:start + size], **fields) for start in range(0, len(text), size)]


def _events(lines: list[dict]):
    provider, ledger = PROVIDERS["claude"], UsageLedger()
    return [event for line in lines if (event := provider.parse(json.dumps(line), ledger)) is not None]


def test_the_command_asks_the_cli_for_partial_messages():
    provider = PROVIDERS["claude"]
    assert "--include-partial-messages" in provider._options(cwd="/w", model=None, mcp_config=None, run_id="r")
    assert "--include-partial-messages" in provider._resume_options("r")


@pytest.mark.parametrize(("lines", "count"), [
    # A paragraph: its first words at once, then a step of its length (between MIN_STEP and MAX_STEP characters) at a time.
    (_words("word " * 400), None),
    # Never thinking, a tool's input, a sub-agent's text.
    ([*_words("hidden " * 40, kind="thinking_delta"), *_words("sub " * 40, parent="tool-1")], 0),
    # A bracket opening a block is held until it closes: a stamp or « no reply » is never shown half-written.
    ([_delta("[no re"), _delta("ply]")], 0),
    ([_delta("[main] Bon"), _delta("jour")], 1),
    # A block this long is clipped by its reader: no more of it is written.
    ([_delta("x" * (PartialBlocks.LIMIT + 1))], 0),
])
def test_only_the_main_threads_visible_text_is_reported_in_steps(lines, count):
    events = _events(lines)
    assert all(event.kind == "partial" for event in events)  # no `other` for a delta line: it would flood the transcript
    if count is None:
        sizes = [len(event.text) for event in events]
        assert sizes[0] <= 25 and sizes == sorted(set(sizes)) and len(events) < len(lines) / 3
        assert all(later - sooner >= PartialBlocks.MIN_STEP for sooner, later in zip(sizes, sizes[1:]))
        assert {event.provider_cursor for event in events} == {"m1:0"}
    else:
        assert len(events) == count
    assert NO_REPLY not in [event.text for event in events]


def test_a_block_closing_names_its_key_and_the_mirror_stamps_it_with_its_first_words(monkeypatch):
    """The live mirror (lines appended in pieces) and a whole rebuild agree, and the answer wears the instant of its first partial."""
    text = "Suspension bridges carry load through cables. " * 12
    stream = [*_words(text), _closing(text), {"type": "result", "subtype": "success", "is_error": False, "session_id": "s"}]
    for run_id in ("live", "whole"):
        register_run(run_id=run_id, name=run_id, provider="claude", task="t")
    mirror, clock = reg.StreamMirror("live"), iter(range(1000, 3000))
    monkeypatch.setattr(reg.time, "time", lambda: float(next(clock)))
    for piece in (stream[:3], stream[3:10], stream[10:-1], stream[-1:]):
        for run_id in ("live", "whole"):
            with reg.raw_events_path(run_id).open("a") as raw:
                raw.write("".join(json.dumps(line) + "\n" for line in piece))
        mirror.refresh()
    reg.read_events("whole")
    for run_id in ("live", "whole"):
        mirrored = [json.loads(line) for line in reg.events_path(run_id).read_text().splitlines()]
        kinds = [event["kind"] for event in mirrored]
        assert kinds[-2:] == ["text", "done"] and set(kinds[:-2]) == {"partial"}, run_id
        said = mirrored[-2]
        assert said["text"] == text and said["provider_cursor"] == "m1:0" and said["at"] == mirrored[0]["at"], run_id
        assert all(text.startswith(event["text"]) and event["provider_cursor"] == "m1:0" for event in mirrored[:-2]), run_id
