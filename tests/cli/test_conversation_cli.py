"""`galaius conversation` and its engine (`galaius.agents.run_ledger`): the invariants a debugging agent relies on. One fake registry
folder, written the way the registry writes it (`<run id>.json` beside its transcript and lock files)."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from galaius.agents import run_slowness
from galaius.agents.run_ledger import RunLedger, RunQuery, RunRow, own_conversation, parse_since, stats
from galaius.agents.run_slowness import DARK_GAP, IDLE_GAP, profile_run, read_transcript, slowness
from galaius.cli.commands.debug import conversation as cli
from galaius.cli.commands.agents import runs as agents_runs

ROOT = "11111111-0000-4000-8000-000000000000"
CHILD = "22222222-0000-4000-8000-000000000000"
GRAND = "33333333-0000-4000-8000-000000000000"
WEB = "44444444-0000-4000-8000-000000000000"
OTHER = "55555555-0000-4000-8000-000000000000"
#: Liveness is read from /proc: where there is none (macOS, Windows) a process is never called gone (`run_ledger.alive`).
needs_proc = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no /proc: a process is never called gone here")
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


@needs_proc
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


def test_a_listing_that_cannot_finish_says_whether_a_lock_holds_it_or_it_is_only_slow(capsys) -> None:
    release = threading.Event()

    def record_lock() -> None:   # a function of this name on the stack is the registry's wait for another process's lock
        release.wait(30)

    def locked() -> None:
        record_lock()

    def slow() -> None:
        release.wait(30)

    for work, said in ((locked, "waiting on a lock another galaius process holds (`record_lock`"), (slow, "no lock on its stack: slow, not stuck")):
        with pytest.raises(SystemExit) as stopped:
            agents_runs._within(0.2, work, "listing the conversation's runs")
        release.set()
        err = capsys.readouterr().err
        assert stopped.value.code == 1 and "took more than 0.2 s" in err and said in err, err
        release.clear()


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
    ({"kind": "conversation", "status": "waiting", "pid": None}, "gone"),                    # resting between turns holds its process too
    ({"kind": "conversation", "status": "waiting", "pid": os.getpid(), "pid_started": None}, "waiting"),
    ({"kind": "process", "status": "running", "pid": os.getpid(), "exit_code": 0}, "done"),   # an exit the record never turned into a status
    ({"kind": "process", "status": "running", "pid": os.getpid(), "exit_code": -15}, "stopped"),
    ({"kind": "process", "status": "starting", "pid": None}, "starting"),                      # still launching: no pid yet
], ids=["conversation-no-process", "conversation-waiting-no-process", "conversation-waiting", "exit-zero", "terminated", "launching"])
def test_a_status_reads_as_the_registry_lists_it(folder: Path, fields: dict, status: str) -> None:
    put(folder, CHILD, parent_run_id=ROOT, **fields)
    assert {row.run_id: row.status for row in RunLedger(folder).rows()}[CHILD] == status


@needs_proc
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


def test_a_run_at_work_survives_since_even_when_its_file_is_old_and_a_finished_old_one_does_not(folder: Path) -> None:
    """A backfill rewrites every file, and a run at work may write nothing for hours: no file time decides, only the record's own."""
    put(folder, CHILD, parent_run_id=ROOT, status="waiting", kind="conversation", pid=os.getpid(), pid_started=None, age=3 * 86400, started_at=time.time() - 5 * 86400, finished_at=None)
    put(folder, GRAND, parent_run_id=CHILD, status="done", age=0, started_at=time.time() - 5 * 86400, finished_at=time.time() - 4 * 86400)  # file new, run old
    found = {row.run_id for row in RunLedger(folder).rows(RunQuery(conversation=ROOT, since=parse_since("24h")))}
    assert CHILD in found and GRAND not in found


@pytest.mark.parametrize("record", [{"started_at": "abc"}, {"session_id": [1]}, {"parent_run_id": 5}, {"stream_digest": {"ending_text": 3, "cut": []}}, {"started_at": 10**30}, {"cost_usd": 10**400}, {"finished_at": float("inf")},
                                    {"usage": {"input": "x"}}, {"cost_usd": "free"}, {"interruption": "oom"}], ids=lambda record: next(iter(record)))
def test_a_field_of_the_wrong_type_in_a_record_or_the_cache_never_stops_a_listing(folder: Path, record: dict) -> None:
    put(folder, CHILD, **{"parent_run_id": ROOT, **record})
    assert CHILD in {row.run_id for row in RunLedger(folder).rows()}   # from the file
    cached = json.loads((folder / ".run-ledger.cache").read_text())
    cached["records"][f"{CHILD}.json"]["record"].update(record)       # the same damage inside the cache, its stamp still matching
    (folder / ".run-ledger.cache").write_text(json.dumps(cached))
    assert CHILD in {row.run_id for row in RunLedger(folder).rows()}


@pytest.mark.parametrize("fields, status", [
    ({"kind": "conversation", "status": "crashed", "pid": None}, "interrupted"),
    ({"kind": "conversation", "status": "starting", "pid": None}, "starting"),
    ({"kind": "process", "status": "starting", "pid": None, "exit_code": 0}, "done"),
    ({"kind": "process", "status": "crashed", "pid": None}, "interrupted"),                     # a status the registry once wrote, now read as interrupted
    ({"kind": "process", "status": "starting", "pid": None, "started_at": 1.0}, "gone"),        # `starting` for 30 years with no process
], ids=["conversation-crashed", "conversation-starting", "starting-with-exit-code", "legacy-crashed", "starting-forever"])
def test_a_status_is_what_the_registry_says_except_for_a_run_coming_back_for_a_queued_message(folder: Path, fields: dict, status: str) -> None:
    put(folder, CHILD, parent_run_id=ROOT, **fields)
    assert {row.run_id: row.status for row in RunLedger(folder).rows()}[CHILD] == status


def test_agents_list_swallows_only_an_unknown_inherited_thread_never_an_ambiguous_one_and_counts_what_it_leaves_out(folder: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    monkeypatch.delenv("GALAIUS_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("GALAIUS_RUN_ID", raising=False)
    put(folder, "66666666-0000-4000-8000-000000000000", session_id="web-ab1")
    put(folder, "77777777-0000-4000-8000-000000000000", session_id="web-ab2")
    monkeypatch.setenv("GALAIUS_SESSION_ID", "web-ab")
    with pytest.raises(SystemExit) as stopped:
        agents_runs.agents_list()
    assert stopped.value.code == 2 and "3 conversations" in capsys.readouterr().err
    monkeypatch.setenv("GALAIUS_SESSION_ID", ROOT)
    agents_runs.agents_list(limit=1)
    out = capsys.readouterr().out
    assert "… 2 older runs not shown" in out and GRAND[:8] in out, "the newest stays; the two older are counted"


def test_the_terminal_statuses_are_the_registry_s_and_a_negative_limit_is_refused(folder: Path, monkeypatch, capsys) -> None:
    from galaius.agents import registry as reg
    from galaius.agents.run_ledger import TERMINAL
    assert TERMINAL == reg.TERMINAL_STATUSES
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    with pytest.raises(SystemExit) as stopped:
        agents_runs.agents_list(all_sessions=True, limit=-5)
    assert stopped.value.code == 2 and "--limit takes 0" in capsys.readouterr().err


def test_a_run_named_as_the_registry_allows_is_read_and_a_json_file_that_is_no_run_is_not(folder: Path) -> None:
    """The registry accepts any `[A-Za-z0-9._:@+-]{1,160}` run id; its folder also holds the quota file, queues and the like."""
    put(folder, "visible-a", session_id="conversation-a")
    (folder / "quota-usage.json").write_text('{"claude": {"blocks": {}}}')
    (folder / "brief-titles.json").write_text('{"run_id": "other"}')
    (folder / "visible-a.queue.json").write_text('{"run_id": "visible-a"}')
    ledger = RunLedger(folder)
    assert {row.run_id for row in ledger.rows(RunQuery(conversation="conversation-a"))} == {"visible-a"}
    assert {row.run_id for row in ledger.rows()} == {ROOT, CHILD, GRAND, WEB, OTHER, "visible-a"}


# --- his messages and why it is slow ---------------------------------------------------------------------------------------


WEB_ID = "36d7fb13-f1cb-4b45-b7cc-cc2416631156"


def _line(kind: str, at: float, **fields) -> str:
    """One transcript line as the registry writes it: compact, `kind` first, `at` last."""
    return json.dumps({"kind": kind, **fields, "at": at}, separators=(",", ":"))


def _transcript(folder: Path, run_id: str, lines: list[str]) -> None:
    (folder / f"{run_id}.jsonl").write_text("\n".join(lines) + "\n")


def _row(run_id: str = CHILD, *, status: str = "done", started: float, finished: float | None):
    return RunRow(run_id=run_id, role="builder", status=status, conversation=ROOT, started_at=started, finished_at=finished)


def test_a_message_is_his_when_the_web_gave_it_its_id_and_each_shows_once_with_what_reads_as_frustration(folder: Path) -> None:
    now = time.time()
    said = lambda event_id, at, text, to=CHILD: json.dumps({"kind": "message", "event_id": event_id, "text": text, "from_run": "operator", "to_run": to, "at": at}, separators=(",", ":"))  # noqa: E731
    resumed = "11111111-2222-4333-8444-555555555556"   # the runtime's own resume brief carries a web-shaped id too
    lines = [said(WEB_ID, now - 60, "toujours pas, wtf"), said("a" * 32, now - 50, "an agent's own agents send"), said(resumed, now - 55, "Resumed after your run was cut (usage limit)"),
             said(WEB_ID, now - 60, "toujours pas, wtf", GRAND), said("11111111-2222-4333-8444-555555555555", now - 30, "fine, thanks", WEB),
             said("11111111-2222-4333-8444-555555555557", now - 20, "Get working !!!!", WEB)]
    (folder / f"{CHILD}.messages.jsonl").write_text("\n".join(lines))
    (folder / f"{GRAND}.messages.jsonl").write_text(lines[3])
    ledger = RunLedger(folder)
    found = ledger.his_messages(since=now - 3600)
    assert [(m.text, bool(m.frustrated), m.conversation) for m in found] == [("toujours pas, wtf", True, ROOT), ("fine, thanks", False, "web-abc"), ("Get working !!!!", True, "web-abc")]
    assert [m.text for m in ledger.his_messages(since=now - 3600, frustrated=True)] == ["toujours pas, wtf", "Get working !!!!"]
    assert [m.text for m in ledger.his_messages(since=now - 3600, conversation=ROOT)] == ["toujours pas, wtf"]
    assert ledger.his_messages(since=now - 25)[0].text == "Get working !!!!" and len(ledger.his_messages(since=now - 25)) == 1, "since cuts by the message's own time"


def test_an_ask_he_made_earlier_in_the_conversation_reads_as_frustration(folder: Path) -> None:
    now = time.time()
    first = "Please show the closed asks in the asks view, with the guard that closed each one of them"
    lines = [json.dumps({"kind": "message", "event_id": f"11111111-2222-4333-8444-55555555555{index}", "text": text, "from_run": "operator", "to_run": CHILD, "at": now - 100 + index})
             for index, text in enumerate([first, "ok", f"again: {first}"])]
    (folder / f"{CHILD}.messages.jsonl").write_text("\n".join(lines))
    found = RunLedger(folder).his_messages(since=now - 3600)
    assert [bool(m.frustrated) for m in found] == [False, False, True] and "raised again" in found[2].frustrated


def test_every_second_of_a_run_lands_in_one_part_and_each_part_is_told_from_the_others(folder: Path) -> None:
    t = 1_000_000.0
    lines = [_line("started", t), _line("thinking", t + 10),
             _line("tool", t + 20, tool="Bash", tool_input="command='galaius agents wait abc'", tool_id="w"), _line("tool_result", t + 320, tool_id="w", text="ok"),
             _line("tool", t + 330, tool="Bash", tool_input="command='make test'", tool_id="b"), _line("tool_result", t + 430, tool_id="b", text="ok"),
             _line("tool", t + 440, tool="Bash", tool_input="command='cd x; deploy_queue.py wait abc'", tool_id="d"), _line("tool_result", t + 500, tool_id="d", text="ok"),
             _line("text", t + 510),
             _line("text", t + 510 + IDLE_GAP + 200),          # this run alone silent past IDLE_GAP: idle
             _line("done", t + 900),
             _line("message", t + 2000),                         # ended, then his next message: the gap is parked
             _line("done", t + 2100)]
    _transcript(folder, CHILD, lines)
    transcript = read_transcript(folder / f"{CHILD}.jsonl", 0)
    profile = profile_run(_row(started=t, finished=t + 2100), transcript, since=t, now=t + 3000, queued=None)
    assert profile.wall == pytest.approx(2100)
    assert profile.waits.agent == pytest.approx(300) and profile.waits.deploy == pytest.approx(60) and profile.tools == pytest.approx(100)
    assert profile.parked == pytest.approx(2000 - 900) and profile.idle == pytest.approx(IDLE_GAP + 200)
    assert profile.wall == pytest.approx(profile.parked + profile.dark + profile.tools + profile.model + profile.idle + profile.waits.total())


def test_a_stretch_in_which_no_run_wrote_anything_is_dark_and_nobody_s_idle_time(folder: Path) -> None:
    t = time.time() - 86400
    _transcript(folder, CHILD, [_line("started", t), _line("text", t + 60), _line("text", t + 60 + DARK_GAP * 4)])
    _transcript(folder, GRAND, [_line("started", t), _line("text", t + 90), _line("text", t + 60 + DARK_GAP * 4 + 30)])
    put(folder, CHILD, parent_run_id=ROOT, started_at=t, finished_at=t + 60 + DARK_GAP * 4)
    put(folder, GRAND, parent_run_id=CHILD, started_at=t, finished_at=t + 60 + DARK_GAP * 4 + 30)
    ledger = RunLedger(folder)
    report = slowness(ledger, ledger.rows(RunQuery(conversation=ROOT)), since=t, now=t + 60 + DARK_GAP * 4 + 40)
    by_id = {p.run_id: p for p in report.runs}
    assert by_id[CHILD].dark > DARK_GAP * 3 and by_id[CHILD].idle <= 30 and by_id[GRAND].dark > DARK_GAP * 3
    assert by_id[CHILD].active() == pytest.approx(by_id[CHILD].wall - by_id[CHILD].dark)


def test_the_time_a_spawned_run_waited_is_read_from_its_parent_s_spawn_call_and_none_when_the_parent_names_no_such_run(folder: Path) -> None:
    t = time.time() - 3600
    _transcript(folder, ROOT, [_line("started", t), _line("tool", t + 10, tool="Bash", tool_input="command='galaius agents spawn --delegate x go'", tool_id="s"),
                               _line("tool_result", t + 11, tool_id="s", text=f"started\n{CHILD}\n"), _line("done", t + 20)])
    put(folder, ROOT, started_at=t, finished_at=t + 20)
    put(folder, CHILD, parent_run_id=ROOT, started_at=t + 131, finished_at=t + 400)       # started 120 s after the call returned: queued
    put(folder, GRAND, parent_run_id=ROOT, started_at=t + 140, finished_at=t + 400)       # no spawn call names it
    ledger = RunLedger(folder)
    report = slowness(ledger, ledger.rows(RunQuery(conversation=ROOT)), since=t - 10, now=t + 500)
    queued = {p.run_id: p.queued for p in report.runs}
    assert queued[CHILD] == pytest.approx(120) and queued[GRAND] is None and report.total.queued_runs == 1


def test_reading_a_transcript_from_a_time_equals_filtering_the_whole_of_it(folder: Path) -> None:
    """Whatever order a transcript's lines are in (an editor session's replays older events in the middle of it), the window holds every line
    whose time is in it, and the turn event before the window is the last one in the file before it."""
    lines = [_line("text", 1000.0 + index, text="x" * 200) for index in range(2000)]
    lines[1000:1000] = [_line("text", 1000.0 + index, text="x" * 200) for index in range(5)]   # replayed from long before
    lines.insert(500, _line("done", 1400.0))
    _transcript(folder, CHILD, lines)
    path = folder / f"{CHILD}.jsonl"
    whole = read_transcript(path, 0).events
    for since in (0.0, 1000.0, 1500.5, 2999.0, 3000.0, 9999.0):
        assert read_transcript(path, since).events == [event for event in whole if event[0] >= since], since
    assert read_transcript(path, 1500.5).lead == (1400.0, "done")


@pytest.mark.parametrize("argv, said", [(["slow", "--since", "5x"], "is no time"), (["messages", "--since", "yesterday"], "is no time"), (["slow", "--conversation", "nope"], "no conversation or run")])
def test_the_new_verbs_refuse_what_they_cannot_read_in_one_error_line(folder: Path, monkeypatch, capsys, argv: list[str], said: str) -> None:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    with pytest.raises(SystemExit) as stopped:
        cli.conversation_app(argv, exit_on_error=False)
    assert stopped.value.code == 2 and said in capsys.readouterr().err


def test_slow_and_messages_print_the_same_numbers_as_json(folder: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(folder))
    now = time.time()
    _transcript(folder, CHILD, [_line("started", now - 600), _line("tool", now - 590, tool="Bash", tool_input="command='sleep 30'", tool_id="s"), _line("tool_result", now - 560, tool_id="s", text=""), _line("done", now - 500)])
    put(folder, CHILD, parent_run_id=ROOT, started_at=now - 600, finished_at=now - 500)
    cli.slow(conversation=ROOT, since="1h", json_out=True)
    report = json.loads(capsys.readouterr().out)
    assert report["slowness"]["total"]["waits"]["timer"] == pytest.approx(30) and "conversation 11111111" in report["covers"]
    cli.slow(conversation=ROOT, since="1h")
    assert "timer 30s" in capsys.readouterr().out
    (folder / f"{CHILD}.messages.jsonl").write_text(json.dumps({"kind": "message", "event_id": WEB_ID, "text": "toujours pas", "from_run": "operator", "to_run": CHILD, "at": now - 10}))
    cli.messages(since="1h", json_out=True)
    assert json.loads(capsys.readouterr().out)["messages"][0]["frustrated"] == "« toujours pas »"


def _tool(t: float, end: float | None, command: str, *, tool: str = "Bash", name: str = "x") -> list[str]:
    lines = [_line("tool", t, tool=tool, tool_input=f"command='{command}'" if tool in ("Bash", "Monitor") else command, tool_id=name)]
    return lines + ([] if end is None else [_line("tool_result", end, tool_id=name, text="ok")])


@pytest.mark.parametrize("tool, command, kind", [
    ("Bash", "galaius agents wait abc --timeout 500", "agent"), ("Bash", "cd /x; timeout 580 ../galaius --python y/deploy_queue.py wait 2026-galaius-x", "deploy"),
    ("Bash", "gh run watch 123", "ci"), ("Bash", "nohup galaius agents wait abc", "agent"), ("Bash", "env -u X FOO=1 galaius agents wait abc", "agent"),
    ("Bash", "$PY ../x/deploy_queue.py wait abc", "deploy"), ("Bash", "PYTHONPATH=x galaius agents wait abc", "agent"), ("Bash", "for i in 1 2; do sleep 10; done", "timer"), ("Bash", "until grep -q x f; do sleep 5; done", "timer"),
    ("AskUserQuestion", "{}", "user"), ("TaskOutput", "{}", "job"),
    # not a wait: the telltale words sit in an argument, or the call returns at once
    ("Bash", "rg \"sleep 5\" src", ""), ("Bash", "echo \"gh run view\"", ""), ("Bash", "git log --grep \"galaius agents wait\"", ""),
    ("Bash", "deploy_queue.py enqueue --summary \"will wait\"", ""), ("Bash", "kill 12; sleep 1; cd x && pytest", ""), ("Bash", "sed -i \"s/told=1; sleep 15/x/\" f", ""), ("Bash", "gh run view 123 --log", ""), ("Bash", "gh pr checks", ""),
    ("Read", "galaius agents wait", ""),
], ids=lambda value: str(value)[:28])
def test_a_tool_call_is_a_wait_by_its_command_not_by_words_in_it(folder: Path, tool: str, command: str, kind: str) -> None:
    t = 1_000_000.0
    _transcript(folder, CHILD, [_line("started", t), *_tool(t + 5, t + 105, command, tool=tool), _line("done", t + 110)])
    profile = profile_run(_row(started=t, finished=t + 110), read_transcript(folder / f"{CHILD}.jsonl", 0), since=t, now=t + 200, queued=None)
    assert (getattr(profile.waits, kind) if kind else profile.tools) == pytest.approx(100), profile


def test_a_call_still_open_counts_to_now_and_a_call_cut_off_by_a_new_turn_ends_with_that_turn(folder: Path) -> None:
    t = 1_000_000.0
    _transcript(folder, CHILD, [_line("started", t), *_tool(t + 10, None, "galaius agents wait abc")])
    open_now = profile_run(_row(status="running", started=t, finished=None), read_transcript(folder / f"{CHILD}.jsonl", 0), since=t, now=t + 1000, queued=None)
    assert open_now.waits.agent == pytest.approx(990) and open_now.idle == 0
    _transcript(folder, CHILD, [_line("started", t), *_tool(t + 10, None, "galaius agents wait abc"), _line("done", t + 100), _line("message", t + 300), _line("done", t + 310)])
    cut = profile_run(_row(started=t, finished=t + 310), read_transcript(folder / f"{CHILD}.jsonl", 0), since=t, now=t + 1000, queued=None)
    assert cut.waits.agent == pytest.approx(90) and cut.parked == pytest.approx(200)


def test_dark_time_is_counted_once_for_the_window_and_beats_a_tool_call_straddling_it(folder: Path) -> None:
    t = time.time() - 36000
    gap = DARK_GAP * 8
    for run_id, parent in ((CHILD, ROOT), (GRAND, ROOT)):
        put(folder, run_id, parent_run_id=parent, started_at=t, finished_at=t + 200 + gap + 50)
    _transcript(folder, CHILD, [_line("started", t), *_tool(t + 100, t + 200 + gap, "make test"), _line("done", t + 200 + gap + 50)])   # a call open across the dark stretch
    _transcript(folder, GRAND, [_line("started", t), _line("text", t + 150), _line("text", t + 200 + gap + 20), _line("done", t + 200 + gap + 50)])
    put(folder, ROOT, started_at=t, finished_at=t + 200 + gap + 50)
    ledger = RunLedger(folder)
    report = slowness(ledger, ledger.rows(RunQuery(conversation=ROOT)), since=t, now=t + 200 + gap + 60)
    by_id = {p.run_id: p for p in report.runs}
    assert DARK_GAP * 7 < report.dark < gap + 100
    assert by_id[CHILD].tools < 200 and by_id[CHILD].dark > DARK_GAP * 7, "the call's time inside the dark stretch is dark, not tools"
    assert all(abs(p.wall - (p.parked + p.dark + p.tools + p.model + p.idle + p.waits.total())) < 1e-6 for p in report.runs)


def test_a_run_waiting_on_an_open_call_with_nobody_else_writing_is_waiting_not_dark(folder: Path) -> None:
    t = time.time() - 1500
    put(folder, CHILD, parent_run_id=ROOT, status="running", pid=os.getpid(), pid_started=None, started_at=t, finished_at=None)
    _transcript(folder, CHILD, [_line("started", t), *_tool(t + 10, None, "galaius agents wait abc")])
    ledger = RunLedger(folder)
    report = slowness(ledger, ledger.rows(RunQuery(conversation=ROOT)), since=t, now=t + 1490)
    worker = next(p for p in report.runs if p.run_id == CHILD)
    assert worker.waits.agent == pytest.approx(1480) and worker.dark == 0 and report.dark == 0


def test_an_ask_he_made_before_the_window_still_makes_his_repeat_in_it_frustrated_and_a_blank_or_the_resume_brief_is_no_message(folder: Path) -> None:
    from galaius.agents import registry as reg
    from galaius.agents.run_ledger import SYSTEM_MESSAGES
    assert all(reg.RESUME_BRIEF.startswith(prefix) for prefix in SYSTEM_MESSAGES), "one source for the runtime's own message"
    now = time.time()
    first = "Please show the closed asks in the asks view, with the guard that closed each one of them"
    said = lambda n, at, text: json.dumps({"kind": "message", "event_id": f"11111111-2222-4333-8444-55555555555{n}", "text": text, "from_run": "operator", "to_run": CHILD, "at": at}, separators=(",", ":"))  # noqa: E731
    (folder / f"{CHILD}.messages.jsonl").write_text("\n".join([said(1, now - 3 * 86400, first), said(2, now - 600, f"again: {first}"), said(3, now - 500, "  "), said(4, now - 400, "")]))
    found = RunLedger(folder).his_messages(since=now - 3600)
    assert [m.text for m in found] == [f"again: {first}"] and "raised again" in found[0].frustrated


def test_a_run_ledger_that_does_not_own_its_folder_writes_no_cache_beside_the_records(folder: Path) -> None:
    RunLedger(folder, cache=False).rows()
    assert not (folder / ".run-ledger.cache").exists()
    RunLedger(folder).rows()
    assert (folder / ".run-ledger.cache").exists()


@pytest.mark.parametrize("ref", ["x" * 5000, "a/b", "../x", "é" * 10, ""])
def test_a_reference_no_run_could_have_is_an_unknown_conversation_never_a_file_error(folder: Path, ref: str) -> None:
    with pytest.raises(LookupError, match="no conversation or run"):
        RunLedger(folder).rows(RunQuery(conversation=ref))
