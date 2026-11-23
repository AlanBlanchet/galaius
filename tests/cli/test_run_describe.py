"""`galaius run <id>` (`galaius.agents.run_describe`): a run read whole from its record, in a fake registry folder written the way the registry writes it."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from galaius.agents import run_describe
from galaius.agents.run_describe import describe, slices_of
from galaius.agents.run_ledger import RunLedger
from galaius.cli.commands import run as cli

ROOT = "11111111-0000-4000-8000-000000000000"
CHILD = "22222222-0000-4000-8000-000000000000"
GRAND = "33333333-0000-4000-8000-000000000000"
DEAD_PID = 2**22 + 12345
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
needs_proc = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no /proc: a process is never called gone here")


def put(folder: Path, run_id: str, **fields) -> None:
    now = time.time()
    record = {"run_id": run_id, "name": "role", "agent": "role", "status": "done", "provider": "claude", "model": "m-a", "started_at": now - 100, "finished_at": now - 50,
              "parent_run_id": None, "session_id": None, "cwd": "/nowhere", "kind": "process", "login": "abcdef0123456789",
              "stream_digest": {"source": "x", "ending_text": f"ended {run_id[:2]}", "cut": None}, **fields}
    (folder / f"{run_id}.json").write_text(json.dumps(record))


def event(folder: Path, run_id: str, kind: str, text: str) -> None:
    with (folder / f"{run_id}.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": kind, "text": text}, separators=(",", ":")) + "\n")


@pytest.fixture
def folder(tmp_path: Path, monkeypatch) -> Path:
    registry = tmp_path / "agents"
    registry.mkdir()
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(registry))
    put(registry, ROOT, agent="main")
    put(registry, CHILD, parent_run_id=ROOT, agent="builder")
    put(registry, GRAND, parent_run_id=CHILD, agent="critic", status="failed", exit_code=1)
    return registry


def test_the_model_it_got_and_why_reads_candidates_what_was_passed_over_and_a_limit_that_refused_it(folder: Path) -> None:
    put(folder, CHILD, parent_run_id=ROOT, agent="builder", requested_criterion="best coder", reasoning="high",
        candidates=[{"provider": "claude", "model": "m-fable", "rank": 0}, {"provider": "claude", "model": "m-a", "rank": 1}],
        skipped=[{"candidate": {"provider": "claude", "model": "m-fable", "rank": 0}, "reason": "quota_exceeded", "message": "window clears later", "until": 1795374060.0}],
        fell_back_from={"model": "m-fable", "pool": "weekly_fable", "resets_at": 1795374060.0})
    why = describe(CHILD, RunLedger(folder)).why_model
    assert (why.chosen, why.chosen_rank, why.candidates, why.criterion, why.reasoning) == ("claude/m-a", 1, 2, "best coder", "high")
    assert [(each.model, each.reason, each.until) for each in why.passed] == [("claude/m-fable", "quota_exceeded", 1795374060.0)]
    assert why.fell_back_from == "m-fable" and why.fell_back_pool == "weekly_fable"


def test_how_it_ended_names_the_cut_the_stopper_the_relaunch_and_the_successor(folder: Path) -> None:
    put(folder, GRAND, parent_run_id=CHILD, status="interrupted", interruption={"cause": "out_of_memory", "at": 1795383460.0, "source": "observed"},
        stopped_by={"kind": "agent", "name": "app-engineer", "run_id": "312d5dbe-810f"}, replaced_by={"run_id": "a23f0d25-56b3", "provider": "claude", "model": None},
        relaunched_from={"provider": "claude", "model": "m-x", "said": "AttributeError: boom", "cause": "fault"}, relaunch_refused="no model left", resumed_cuts=2)
    ended = describe(GRAND, RunLedger(folder)).ended
    assert (ended.cut, ended.cut_source, ended.stopped_by, ended.successor, ended.resumed_cuts) == ("out_of_memory", "observed", "agent app-engineer 312d5dbe", "a23f0d25", 2)
    assert "AttributeError: boom" in ended.relaunched_from and ended.relaunch_refused == "no model left"


def test_the_tree_shows_parents_above_and_every_descendant_below_with_its_depth(folder: Path) -> None:
    mid = describe(CHILD, RunLedger(folder))
    assert [each.run_id for each in mid.ancestors] == [ROOT]
    assert [(each.run_id, each.depth, each.status) for each in mid.tree] == [(GRAND, 0, "failed")]
    top = describe(ROOT[:8], RunLedger(folder))
    assert [(each.run_id, each.depth) for each in top.tree] == [(CHILD, 0), (GRAND, 1)], "an 8-character id names the run"


def test_the_queue_lists_what_is_not_settled_with_its_error_and_counts_the_rest(folder: Path) -> None:
    items = [{"state": "replied", "sender": "a", "enqueued_at": 1.0}, {"state": "replied", "sender": "a", "enqueued_at": 2.0},
             {"state": "failed", "sender": "cut-resume", "enqueued_at": 3.0, "error": "the resumed turn did not start"},
             {"state": "uncertain", "sender": "cut-resume", "enqueued_at": 4.0, "error": "acceptance was not confirmed"}]
    (folder / f"{CHILD}.queue.json").write_text(json.dumps({"version": 1, "items": items}))
    queue = describe(CHILD, RunLedger(folder)).queue
    assert queue.counts == {"replied": 2, "failed": 1, "uncertain": 1}
    assert [(each.state, each.error) for each in queue.unsettled] == [("failed", "the resumed turn did not start"), ("uncertain", "acceptance was not confirmed")]
    assert describe(ROOT, RunLedger(folder)).queue.counts == {}, "no queue file is no queue"


def test_the_report_is_the_last_done_or_error_event_clipped_to_the_asked_length(folder: Path) -> None:
    event(folder, CHILD, "text", "thinking out loud")
    event(folder, CHILD, "done", "x" * 300)
    report = describe(CHILD, RunLedger(folder), report_chars=50).report
    assert report.event == "done" and report.chars == 300 and report.text == "x" * 50


@needs_proc
def test_a_pid_is_judged_only_for_a_run_at_work(folder: Path) -> None:
    put(folder, GRAND, parent_run_id=CHILD, status="running", pid=DEAD_PID)
    assert describe(GRAND, RunLedger(folder)).process_alive is False
    put(folder, GRAND, parent_run_id=CHILD, status="done", pid=DEAD_PID)
    assert describe(GRAND, RunLedger(folder)).process_alive is None


def test_an_id_no_run_has_or_a_prefix_two_runs_share_is_refused(folder: Path) -> None:
    with pytest.raises(LookupError, match="no run 'deadbeef'"):
        describe("deadbeef", RunLedger(folder))
    put(folder, "11111111-1111-4000-8000-000000000001")
    with pytest.raises(LookupError, match="names 2 runs"):
        describe("11111111", RunLedger(folder))


def test_the_command_errors_with_exit_2_for_an_unknown_id_and_reads_its_own_run_with_no_id(folder: Path, monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.run("deadbeef", slice_check=False)
    assert stopped.value.code == 2 and "no run 'deadbeef'" in capsys.readouterr().err
    monkeypatch.setenv("GALAIUS_RUN_ID", CHILD)
    cli.run(slice_check=False)
    assert capsys.readouterr().out.startswith(f"{CHILD} builder · done")
    cli.run(CHILD, json_out=True, slice_check=False)
    assert json.loads(capsys.readouterr().out)["run"]["run_id"] == CHILD
    monkeypatch.delenv("GALAIUS_RUN_ID")
    with pytest.raises(SystemExit) as stopped:
        cli.run(slice_check=False)
    assert stopped.value.code == 2


# --- is its slice live -------------------------------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, str]:
    folder = tmp_path / "checkout"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "main")
    shas = {}
    for name in ("first", "live", "later"):
        (folder / "f").write_text(name)
        git(folder, "add", "f")
        git(folder, "commit", "-q", "-m", name)
        shas[name] = git(folder, "rev-parse", "HEAD")
    return {"cwd": str(folder), **shas}


@needs_git
def test_a_commit_the_report_names_is_live_when_the_apps_build_reaches_it_and_not_live_when_it_does_not(repo: dict[str, str]) -> None:
    build = f"20261122T232915Z-{repo['live'][:7]}"
    report = f"shipped {repo['first'][:9]} and then {repo['later'][:9]}; also deadbeefcafe which no checkout holds"
    found = {each.commit: each for each in slices_of(report, repo["cwd"], lambda: build)}
    assert found[repo["first"][:8]].verdict == "live" and "reachable from" in found[repo["first"][:8]].why
    assert found[repo["later"][:8]].verdict == "not live"
    assert len(found) == 2, "a hash this checkout does not hold is not a commit of the run"


@needs_git
def test_an_unreadable_build_or_one_this_checkout_lacks_is_unknown_never_live(repo: dict[str, str]) -> None:
    assert [each.verdict for each in slices_of(repo["first"][:9], repo["cwd"], lambda: None)] == ["unknown"]
    assert [each.verdict for each in slices_of(repo["first"][:9], repo["cwd"], lambda: "2026-1234567")] == ["unknown"]


@needs_git
def test_the_apps_build_is_asked_only_when_the_report_names_a_commit(repo: dict[str, str]) -> None:
    def never() -> str:
        raise AssertionError("the app was asked for nothing")
    assert slices_of("no hash here, only 1234567 digits", repo["cwd"], never) == []
    assert slices_of(repo["first"][:9], "", never) == []


def test_a_commit_shape_needs_a_digit_and_a_letter() -> None:
    assert run_describe._COMMIT.findall("1234567 abcdefa 9f3b2c1 deadbeef") == ["9f3b2c1"]
