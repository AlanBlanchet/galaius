"""What an older runtime wrote on this PC — its events, run records and media usage log, the WHOLE prompt as
`input_tokens` with its cache reads and writes inside it — reads as `TokenCounts` once, at the model: never an old
record read as « nothing counted », never a cached token counted twice."""
import json

import pytest
from galaius_core import TokenCounts

from galaius.agents.transport.events import AgentEvent
from galaius.agents import registry as reg
from galaius.agents.registry import AgentRun
from galaius.vision.usage_records import UsageEntry

RUN = {"run_id": "8f0c2a4e-55b1-4c4e-9d43-0f6f1c1d2e3a", "provider": "claude", "name": "worker", "started_at": 1.0, "status": "done"}
ENTRY = {"timestamp": "2026-10-20T10:00:00Z", "model": "gpt-5", "backend": "api", "provider": "openai", "billing": "metered_api", "outcome": "succeeded"}


@pytest.mark.parametrize(("model", "written", "usage"), [
    (AgentEvent, {"kind": "done", "input_tokens": 5000, "output_tokens": 70, "cached_input_tokens": 4000, "cache_write_input_tokens": 600},
     TokenCounts(input=400, output=70, cache_read=4000, cache_write=600)),
    (AgentEvent, {"kind": "text", "input_tokens": 120, "cached_input_tokens": 100, "cache_write_input_tokens": 0}, TokenCounts(input=20, cache_read=100, cache_write=0)),
    (AgentEvent, {"kind": "done", "usage": {"input": 400, "output": 70, "cache_read": 4000}}, TokenCounts(input=400, output=70, cache_read=4000)),
    (AgentEvent, {"kind": "tool"}, None),
    (AgentRun, {**RUN, "input_tokens": 90000, "output_tokens": 3000, "cached_input_tokens": 85000, "cache_write_input_tokens": 1000},
     TokenCounts(input=4000, output=3000, cache_read=85000, cache_write=1000)),
    (AgentRun, {**RUN, "input_tokens": None, "output_tokens": None, "cached_input_tokens": None, "cache_write_input_tokens": None}, None),
    (UsageEntry, {**ENTRY, "input_tokens": 1500, "output_tokens": 200}, TokenCounts(input=1500, output=200)),
])
def test_an_older_runtimes_record_reads_as_token_counts(model, written: dict, usage: TokenCounts | None) -> None:
    read = model.model_validate(written)
    assert read.usage == (usage if usage is not None or model is not UsageEntry else TokenCounts())
    assert not read.model_dump(mode="json").keys() & {"input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens"}


def test_an_older_rate_limit_lines_windows_read_as_its_windows() -> None:
    window = {"window": "five_hour", "used_percent": 40.0, "observed_at": "2026-10-20T10:00:00Z"}
    event = AgentEvent.model_validate({"kind": "rate_limit", "usage": [window]})
    assert event.usage is None and [w.window for w in event.windows] == ["five_hour"]


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
