"""Token counts as each source spells them — a vendor's usage report, what an older runtime wrote on this PC (its events,
run records and media usage log, the WHOLE prompt as `input_tokens` with its cache reads and writes inside it) — read as
`TokenCounts` once, at the edge: never a record read as « nothing counted », never a cached token counted twice."""
import json

import pytest
from galaius_core import TokenCounts

from galaius.agents.transport.events import AgentEvent
from galaius.agents import registry as reg
from galaius.agents.registry import AgentRun
from galaius.agents.providers import ClaudeCodeProvider, CodexProvider
from galaius.agents.transport.events import UsageLedger
from galaius.agents.transport.codex import _CodexTransport
from galaius.usage_dialects import LITELLM, OLDER_RUNTIME, UsageDialect
from galaius.vision.usage_records import UsageEntry

RUN = {"run_id": "8f0c2a4e-55b1-4c4e-9d43-0f6f1c1d2e3a", "provider": "claude", "name": "worker", "started_at": 1.0, "status": "done"}
ENTRY = {"timestamp": "2026-10-20T10:00:00Z", "model": "gpt-5", "backend": "api", "provider": "openai", "billing": "metered_api", "outcome": "succeeded"}


@pytest.mark.parametrize(("model", "written", "usage"), [
    (AgentEvent, {"kind": "done", "input_tokens": 5000, "output_tokens": 70, "cached_input_tokens": 4000, "cache_write_input_tokens": 600},
     TokenCounts(input=400, output=70, cache_read=4000, cache_write=600)),
    (AgentEvent, {"kind": "text", "input_tokens": 120, "cached_input_tokens": 100, "cache_write_input_tokens": 0}, TokenCounts(input=20, cache_read=100, cache_write=0)),
    (AgentEvent, {"kind": "done", "usage": {"input": 400, "output": 70, "cache_read": 4000}}, TokenCounts(input=400, output=70, cache_read=4000)),
    (AgentEvent, {"kind": "tool"}, TokenCounts()),
    (AgentRun, {**RUN, "input_tokens": 90000, "output_tokens": 3000, "cached_input_tokens": 85000, "cache_write_input_tokens": 1000},
     TokenCounts(input=4000, output=3000, cache_read=85000, cache_write=1000)),
    (AgentRun, {**RUN, "input_tokens": None, "output_tokens": None, "cached_input_tokens": None, "cache_write_input_tokens": None}, TokenCounts()),
    (UsageEntry, {**ENTRY, "input_tokens": 1500, "output_tokens": 200}, TokenCounts(input=1500, output=200)),
])
def test_an_older_runtimes_record_reads_as_token_counts(model, written: dict, usage: TokenCounts) -> None:
    read = model.model_validate(written)
    assert read.usage == usage
    assert not read.model_dump(mode="json").keys() & OLDER_RUNTIME.names


class _Usage:
    """A litellm usage object: figures as attributes, its cache reads one level down."""
    prompt_tokens, completion_tokens, prompt_tokens_details = 5000, 70, type("Details", (), {"cached_tokens": 4000})()


@pytest.mark.parametrize(("dialect", "report", "usage"), [
    (ClaudeCodeProvider.USAGE, {"input_tokens": 2, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 300, "output_tokens": 9},
     TokenCounts(input=2, output=9, cache_read=100000, cache_write=300)),
    (ClaudeCodeProvider.MODEL_USAGE, {"inputTokens": 2, "cacheReadInputTokens": 7, "outputTokens": 1}, TokenCounts(input=2, output=1, cache_read=7)),
    (_CodexTransport.USAGE, {"inputTokens": 5000, "cachedInputTokens": 4000, "outputTokens": 70}, TokenCounts(input=1000, output=70, cache_read=4000)),
    (CodexProvider.USAGE, {"input_tokens": 5000, "cached_input_tokens": 4000, "output_tokens": 70}, TokenCounts(input=1000, output=70, cache_read=4000)),
    (LITELLM, {"usage": {"prompt_tokens": 5000, "completion_tokens": 70, "prompt_tokens_details": {"cached_tokens": 4000}, "cache_creation_input_tokens": 600}},
     TokenCounts(input=400, output=70, cache_read=4000, cache_write=600)),
    (LITELLM, {"usage": {"prompt_tokens": 5000, "completion_tokens": 70, "prompt_tokens_details": {"cached_tokens": 4000}}},
     TokenCounts(input=1000, output=70, cache_read=4000)),
    (LITELLM, type("Response", (), {"usage": _Usage()})(), TokenCounts(input=1000, output=70, cache_read=4000)),
    (LITELLM, {"usage": {"input_tokens": 12, "output_tokens": 3}}, TokenCounts(input=12, output=3)),
    (LITELLM, {"usage": None}, TokenCounts()),
    (ClaudeCodeProvider.USAGE, {"input_tokens": True, "output_tokens": 4}, TokenCounts(output=4)),
    (ClaudeCodeProvider.USAGE, "not a report", TokenCounts()),
])
def test_a_vendor_report_reads_as_token_counts(dialect: UsageDialect, report: object, usage: TokenCounts) -> None:
    assert dialect.read(report) == usage


def test_an_older_rate_limit_lines_windows_read_as_its_windows() -> None:
    window = {"window": "five_hour", "used_percent": 40.0, "observed_at": "2026-10-20T10:00:00Z"}
    event = AgentEvent.model_validate({"kind": "rate_limit", "usage": [window]})
    assert not event.usage.reported and [w.window for w in event.windows] == ["five_hour"]


def test_an_older_runs_files_on_this_pc_keep_their_counts_and_are_written_back_in_the_one_spelling() -> None:
    """The record and its event lines as an older runtime left them in the run folder: read through the registry with
    every token kept, and the next event folded onto it writes the record back with `usage` alone."""
    run_id = RUN["run_id"]
    reg.agents_dir().mkdir(parents=True, exist_ok=True)
    (reg.agents_dir() / f"{run_id}.json").write_text(json.dumps(
        {**RUN, "status": "running", "input_tokens": 5000, "output_tokens": 70, "cached_input_tokens": 4000, "cache_write_input_tokens": 600}))
    reg.events_path(run_id).write_text(json.dumps(
        {"kind": "done", "input_tokens": 5000, "output_tokens": 70, "cached_input_tokens": 4000, "cache_write_input_tokens": 600}) + "\n")
    older = TokenCounts(input=400, output=70, cache_read=4000, cache_write=600)
    assert [event.usage for event in reg.read_events(run_id)] == [older] and reg.get_run(run_id).usage == older
    reg.append_event(run_id, AgentEvent(kind="text", usage=TokenCounts(input=10, cache_read=4600)))
    written = json.loads((reg.agents_dir() / f"{run_id}.json").read_text())
    assert written["usage"] == {"input": 410, "output": 70, "cache_read": 8600, "cache_write": 600}
    assert not written.keys() & {"input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens"}


def test_a_line_without_usage_never_marks_its_message_counted() -> None:
    """Claude repeats a message on each content block's line: one without usage leaves the message's count to the next."""
    provider, ledger = ClaudeCodeProvider(), UsageLedger()
    line = {"type": "assistant", "session_id": "s", "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
    first = provider.parse(json.dumps(line), ledger)
    line["message"]["usage"] = {"input_tokens": 3, "cache_read_input_tokens": 900, "output_tokens": 1}
    second = provider.parse(json.dumps(line), ledger)
    assert not first.usage.reported and second.usage == TokenCounts(input=3, cache_read=900) and second.context_tokens == 903
