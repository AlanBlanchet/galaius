"""`galaius conversation` and its engine (`galaius.agents.run_ledger`): the invariants a debugging agent relies on. One fake registry
folder, written the way the registry writes it (`<run id>.json` beside its transcript and lock files)."""

import json
import os
import time
from pathlib import Path

import pytest

from galaius.agents.run_ledger import RunLedger, RunQuery, own_conversation, parse_since, stats
from galaius.cli.commands import conversation as cli
from galaius.cli.commands.agents import runs as agents_runs

ROOT = "11111111-0000-4000-8000-000000000000"
CHILD = "22222222-0000-4000-8000-000000000000"
GRAND = "33333333-0000-4000-8000-000000000000"
WEB = "44444444-0000-4000-8000-000000000000"
OTHER = "55555555-0000-4000-8000-000000000000"
DEAD_PID = 2**22 + 12345  # beyond pid_max on Linux defaults


def put(folder: Path, run_id: str, *, age: float = 0.0, **fields) -> None:
    """A run record last written `age` seconds ago."""
    now = time.time()
    record = {"run_id": run_id, "name": "role", "agent": "role", "status": "done", "provider": "claude", "model": "m", "started_at": now - age - 5, "finished_at": now - age,
              "parent_run_id": None, "session_id": None, "pid": None, "stream_digest": {"source": "x", "ending_text": f"said {run_id[:2]}", "cut": None}, **fields}
    path = folder / f"{run_id}.json"
    path.write_text(json.dumps(record))
    os.utime(path, (now - age, now - age))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    registry = tmp_path / ".galaius" / "out" / "agents"
    registry.mkdir(parents=True, exist_ok=True)
    put(registry, ROOT, agent="main")
    put(registry, CHILD, parent_run_id=ROOT, agent="builder", cost_usd=1.5, usage={"input": 10, "output": 20, "cache_read": 30, "cache_write": 40})
    put(registry, GRAND, parent_run_id=CHILD, agent="critic", status="failed", stream_digest={"source": "x", "ending_text": "refused", "cut": "usage_limit"}, cost_usd=None)
    put(registry, WEB, session_id="web-abc", agent="blob")
    put(registry, OTHER, agent="main")
    for noise in (f"{CHILD}.lock", f"{CHILD}.raw.jsonl", f"{CHILD}.steps.jsonl", "00000000.lock", f"{CHILD}.queue.json"):
        (registry / noise).write_text("{}")
    return registry


def test_a_conversation_is_the_session_else_the_parent_chain_root(folder: Path) -> None:
    ledger = RunLedger(folder)
    assert {row.run_id for row in ledger.rows(RunQuery(conversation=ROOT))} == {ROOT, CHILD, GRAND}, "descendants of a root with no session id"
    assert ledger.conversation_of(GRAND[:8]) == ROOT, "an 8-character id names a run, whose conversation it is"
    assert {row.run_id for row in ledger.rows(RunQuery(conversation="web-abc"))} == {WEB}
    assert {row.run_id for row in ledger.rows(RunQuery(conversation=GRAND))} == {ROOT, CHILD, GRAND}, "any run id of the conversation"


def test_a_conversation_nobody_has_is_refused_and_an_ambiguous_prefix_names_its_candidates(folder: Path) -> None:
    ledger = RunLedger(folder)
    with pytest.raises(LookupError, match="no conversation or run"):
        ledger.rows(RunQuery(conversation="deadbeef"))
    put(folder, "11111111-1111-4000-8000-000000000001", session_id="web-abd")
    with pytest.raises(LookupError, match="2 conversations"):
        ledger.rows(RunQuery(conversation="web-ab"))


def test_only_run_records_are_read_never_locks_streams_or_queues(folder: Path) -> None:
    assert {row.run_id for row in RunLedger(folder).rows()} == {ROOT, CHILD, GRAND, WEB, OTHER}


def test_since_skips_files_older_than_it_and_keeps_a_run_that_ended_inside_it(folder: Path) -> None:
    put(folder, CHILD, age=7200, parent_run_id=ROOT)
    put(folder, GRAND, age=600, parent_run_id=CHILD, started_at=time.time() - 9000)  # began long ago, wrote 10 minutes ago
    recent = {row.run_id for row in RunLedger(folder).rows(RunQuery(conversation=ROOT, since=parse_since("1h")))}
    assert recent == {ROOT, GRAND}


@pytest.mark.parametrize("text, seconds", [("90s", 90), ("30m", 1800), ("6h", 21600), ("2d", 172800), ("1.5h", 5400)])
def test_since_reads_a_length_of_time(text: str, seconds: int) -> None:
    assert parse_since(text, now=1_000_000.0) == 1_000_000.0 - seconds


@pytest.mark.parametrize("text", ["", "yesterday", "5x", "h6"])
def test_since_refuses_what_is_no_time(text: str) -> None:
    with pytest.raises(ValueError, match="is no time"):
        parse_since(text)


def test_a_record_at_work_whose_process_is_gone_reads_gone_and_one_alive_reads_running(folder: Path) -> None:
    put(folder, CHILD, parent_run_id=ROOT, status="running", pid=DEAD_PID, pid_started=1)
    put(folder, GRAND, parent_run_id=CHILD, status="running", pid=os.getpid(), pid_started=None)
    status = {row.run_id: row.status for row in RunLedger(folder).rows(RunQuery(conversation=ROOT))}
    assert status[CHILD] == "gone" and status[GRAND] == "running"
    assert {row.run_id for row in RunLedger(folder).rows(RunQuery(statuses=frozenset({"gone"})))} == {CHILD}


def test_a_cut_is_a_filter_and_a_count(folder: Path) -> None:
    cut = RunLedger(folder).rows(RunQuery(cut=True))
    assert [(row.run_id, row.cut) for row in cut] == [(GRAND, "usage_limit")]
    done = stats(RunLedger(folder).rows(RunQuery(conversation=ROOT)))
    assert done.runs == 3 and done.cuts == {"usage_limit": 1}
    assert done.cost_usd == 1.5 and done.unpriced == 2, "a run with no price is counted apart, never as zero spend"
    assert done.tokens.output == 20 and {group.name: group.runs for group in done.by_status} == {"done": 2, "failed": 1}


def test_a_system_cut_reads_from_the_interruption_when_the_stream_named_none(folder: Path) -> None:
    put(folder, CHILD, parent_run_id=ROOT, status="interrupted", interruption={"cause": "out_of_memory", "at": 1.0, "source": "journal"})
    assert RunLedger(folder).rows(RunQuery(statuses=frozenset({"interrupted"})))[0].cut == "out_of_memory"


def test_last_words_is_the_last_ending_event_else_its_latest_words_else_the_record(folder: Path) -> None:
    event = lambda kind, text: json.dumps({"kind": kind, "text": text}, separators=(",", ":"))  # the registry writes compact lines  # noqa: E731
    (folder / f"{CHILD}.jsonl").write_text("\n".join([event("text", "midway"), '{"kind":"done","text":"first report"}', event("text", "again"),
                                                      '{"kind":"done","text":"final report"}', event("rate_limit", "noise")]))
    ledger = RunLedger(folder)
    ending = ledger.last_words(CHILD[:8])
    assert (ending.event, ending.text, ending.chars) == ("done", "final report", 12)
    assert ledger.last_words(CHILD, text_chars=5).text == "final" and ledger.last_words(CHILD, text_chars=5).chars == 12
    (folder / f"{GRAND}.jsonl").write_text(event("text", "still going"))
    assert (ledger.last_words(GRAND).event, ledger.last_words(GRAND).text) == ("text", "still going")
    assert ledger.last_words(ROOT).event == "" and ledger.last_words(ROOT).digest == "said 11"
    with pytest.raises(LookupError):
        ledger.last_words("deadbeef")


def test_a_run_asking_reads_its_own_conversation_and_a_person_with_none_reads_nothing_guessed(folder: Path) -> None:
    assert own_conversation({"GALAIUS_RUN_ID": GRAND}, folder) == ROOT
    assert own_conversation({"GALAIUS_PARENT_RUN_ID": CHILD}, folder) == ROOT
    assert own_conversation({"GALAIUS_RUN_ID": "99999999-0000-4000-8000-000000000000", "GALAIUS_SESSION_ID": "web-abc"}, folder) == "web-abc"
    assert own_conversation({}, folder) is None


def test_recent_defaults_to_the_asking_runs_conversation_as_a_tree_and_all_widens_it(folder: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    monkeypatch.setenv("GALAIUS_RUN_ID", GRAND)
    cli.recent()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("conversation 11111111") and "3 runs" in lines[0]
    body = [line for line in lines[1:] if line.strip()]
    assert [line.split()[2] for line in body] == [ROOT[:8], CHILD[:8], GRAND[:8]], "parent before child"
    assert "cut: usage_limit — refused" in body[2]
    cli.recent(every=True, json_out=True)
    shown = json.loads(capsys.readouterr().out)
    assert {run["run_id"] for run in shown["runs"]} == {ROOT, CHILD, GRAND, WEB, OTHER} and "every conversation" in shown["covers"]


@pytest.mark.parametrize("call, said", [
    (lambda: cli.recent(since="5x"), "is no time"),
    (lambda: cli.recent(conversation="nope", every=True), "choose --conversation or --all"),
    (lambda: cli.recent(conversation="zzzzzzzz"), "no conversation or run"),
    (lambda: cli.last(["deadbeef"]), "no run 'deadbeef'"),
], ids=["bad-since", "both-scopes", "unknown-conversation", "unknown-run"])
def test_a_bad_option_is_one_error_line_and_exit_2(folder: Path, monkeypatch, capsys, call, said: str) -> None:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    with pytest.raises(SystemExit) as stopped:
        call()
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR: ") and said in err and len(err.splitlines()) == 1


def test_stats_prints_the_axes_and_json_carries_the_same_numbers(folder: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    cli.stats_command(conversation=ROOT)
    text = capsys.readouterr().out
    assert "3 runs" in text and "usage_limit 1" in text and "role:" in text and "login:" in text
    cli.stats_command(conversation=ROOT, json_out=True)
    assert json.loads(capsys.readouterr().out)["stats"]["cuts"] == {"usage_limit": 1}


def test_a_listing_that_cannot_finish_says_where_it_is_stuck_instead_of_hanging(capsys) -> None:
    release = __import__("threading").Event()

    def wait_for_a_lock() -> None:
        release.wait(30)

    with pytest.raises(SystemExit) as stopped:
        agents_runs._within(0.2, wait_for_a_lock, "listing the conversation's runs")
    release.set()
    err = capsys.readouterr().err
    assert stopped.value.code == 1 and "took more than 0.2 s" in err and "is still in wait" in err


def test_agents_list_finds_a_root_conversation_that_never_had_a_session_id(folder: Path, monkeypatch, capsys) -> None:
    """2026-11-21: `agents list` answered « Session identity required » from inside any run whose conversation root has no session id."""
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    monkeypatch.setenv("GALAIUS_RUN_ID", GRAND)
    monkeypatch.delenv("GALAIUS_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("GALAIUS_SESSION_ID", raising=False)
    agents_runs.agents_list()
    out = capsys.readouterr().out
    assert out.startswith(f"Session: {ROOT}") and CHILD[:8] in out and GRAND[:8] in out and WEB[:8] not in out


def test_the_cache_serves_unchanged_records_and_never_a_stale_or_broken_one(folder: Path) -> None:
    first = RunLedger(folder).rows()
    assert (folder / ".run-ledger.cache").exists(), "the cache sits beside the registry as a dot file, which its listings skip"
    assert RunLedger(folder).rows() == first
    put(folder, CHILD, parent_run_id=ROOT, agent="renamed", age=-5)  # rewritten: its file moved
    assert {row.run_id: row.role for row in RunLedger(folder).rows()}[CHILD] == "renamed"
    (folder / ".run-ledger.cache").write_text("{not json")
    assert {row.run_id for row in RunLedger(folder).rows()} == {row.run_id for row in first}, "a broken cache is no cache"
    assert not any(name.startswith(".run-ledger.cache.") for name in os.listdir(folder)), "no temporary file left behind"


def test_a_run_at_work_is_never_dropped_by_since_however_long_ago_it_started(folder: Path) -> None:
    """A run working for 30 h writes its record with every event: `agents list` (24 h default) and `recent` must still show it."""
    put(folder, CHILD, parent_run_id=ROOT, status="running", pid=os.getpid(), pid_started=None, started_at=time.time() - 30 * 3600, finished_at=None)
    shown = {row.run_id: row.status for row in RunLedger(folder).rows(RunQuery(conversation=ROOT, since=parse_since("24h")))}
    assert shown[CHILD] == "running"
    put(folder, GRAND, parent_run_id=CHILD, age=3 * 86400, started_at=time.time() - 4 * 86400)
    assert GRAND not in {row.run_id for row in RunLedger(folder).rows(RunQuery(conversation=ROOT, since=parse_since("24h")))}


@pytest.mark.parametrize("fields, status", [
    ({"kind": "conversation", "status": "running", "pid": None}, "gone"),                     # a conversation holds its process from its start
    ({"kind": "conversation", "status": "waiting", "pid": None}, "waiting"),
    ({"kind": "process", "status": "running", "pid": os.getpid(), "exit_code": 0}, "done"),   # an exit the record never turned into a status
    ({"kind": "process", "status": "running", "pid": os.getpid(), "exit_code": -15}, "stopped"),
    ({"kind": "process", "status": "starting", "pid": None}, "starting"),                      # still launching: no pid yet
], ids=["conversation-no-process", "conversation-waiting", "exit-zero", "terminated", "launching"])
def test_a_status_reads_as_the_registry_lists_it(folder: Path, fields: dict, status: str) -> None:
    put(folder, CHILD, parent_run_id=ROOT, **fields)
    assert {row.run_id: row.status for row in RunLedger(folder).rows()}[CHILD] == status


def test_a_run_its_provider_hosts_is_as_alive_as_its_host(folder: Path) -> None:
    put(folder, CHILD, parent_run_id=ROOT, status="running", pid=DEAD_PID, pid_started=1)
    put(folder, GRAND, parent_run_id=CHILD, kind="provider_child", status="running")
    assert {row.run_id: row.status for row in RunLedger(folder).rows()}[GRAND] == "gone"
    put(folder, CHILD, parent_run_id=ROOT, status="running", pid=os.getpid(), pid_started=None)
    assert {row.run_id: row.status for row in RunLedger(folder).rows()}[GRAND] == "running"


def test_a_forgotten_run_leaves_nothing_in_the_cache_and_a_rewrite_in_one_clock_tick_is_not_served_stale(folder: Path) -> None:
    RunLedger(folder).rows()
    assert "refused" in (folder / ".run-ledger.cache").read_text()
    before = os.stat(folder / f"{GRAND}.json")
    put(folder, GRAND, parent_run_id=CHILD, stream_digest={"source": "x", "ending_text": "refuseZ", "cut": "usage_limit"})  # same size, then the old time back
    os.utime(folder / f"{GRAND}.json", ns=(before.st_atime_ns, before.st_mtime_ns))
    assert {row.run_id: row.said for row in RunLedger(folder).rows()}[GRAND] == "refuseZ"
    (folder / f"{GRAND}.json").unlink()
    RunLedger(folder).rows()
    assert "refuseZ" not in (folder / ".run-ledger.cache").read_text()


@pytest.mark.parametrize("broken", ['{"version": 2, "records": []}', '{"version": 2, "records": {"x.json": 5}}',
                                    '{"version": 2, "records": {"' + CHILD + '.json": {"stamp": 1, "record": []}}}', '[]'])
def test_a_cache_of_the_wrong_shape_is_no_cache(folder: Path, broken: str) -> None:
    (folder / ".run-ledger.cache").write_text(broken)
    assert {row.run_id for row in RunLedger(folder).rows()} == {ROOT, CHILD, GRAND, WEB, OTHER}


def test_a_reference_that_is_a_conversation_and_a_run_of_another_is_refused(folder: Path) -> None:
    put(folder, "66666666-0000-4000-8000-000000000000", parent_run_id=ROOT)
    put(folder, "77777777-0000-4000-8000-000000000000", session_id="66666666-zz")
    with pytest.raises(LookupError, match="both a conversation"):
        RunLedger(folder).rows(RunQuery(conversation="66666666"))


def test_agents_list_every_session_goes_through_the_ledger_and_an_unknown_harness_thread_lists_nothing(folder: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    monkeypatch.delenv("GALAIUS_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("GALAIUS_RUN_ID", raising=False)
    agents_runs.agents_list(all_sessions=True)
    out = capsys.readouterr().out
    assert out.startswith("All sessions") and all(run_id[:8] in out for run_id in (ROOT, WEB, OTHER))
    monkeypatch.setenv("GALAIUS_SESSION_ID", "thread-without-records")
    agents_runs.agents_list()
    assert "No agent runs" in capsys.readouterr().out
    with pytest.raises(SystemExit) as stopped:
        agents_runs.agents_list(session_id="thread-without-records")
    assert stopped.value.code == 2, "a conversation he NAMED that nothing holds is an error; only an inherited one lists empty"
