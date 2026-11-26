"""The agent-efficiency check (`galaius.agents.history.run_efficiency`): one guard row per way a run loses time or capability to something other than its
task: a start below its role's normal, bookkeeping calls, a wait for a place, a vendor-error end, a model or effort below the role's rule. Each is
read from a fake registry folder written the way the registry writes it; a row passes only when its finding names the run."""

import json
import time
from pathlib import Path

import pytest

from galaius.agents import run_slowness
from galaius.agents.history import run_efficiency as eff
from galaius.agents.history.run_efficiency import EfficiencyLimits, Pick, StartKit, efficiency, findings, look, read_pick, read_start, vendor_end
from galaius.agents.run_ledger import RunLedger, RunQuery, RunRow
from galaius.agents.run_slowness import Slowness, bookkeeping_label, profile_run, read_transcript, rollup
from galaius.cli.commands.debug import conversation as cli
from galaius.diagnostics import health

NOW = 1_800_000_000.0


def run_id(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def row(n: int, *, role: str = "backend-engineer", status: str = "done", **fields) -> RunRow:
    return RunRow(run_id=run_id(n), role=role, status=status, provider="claude", model="m", conversation="c", started_at=NOW - 3600, finished_at=NOW - 60, **fields)


KIT = StartKit(tools=141, mcp=6, skills=60, memory=True)
kits = lambda mapping: (lambda _root, rid: mapping.get(rid, KIT))  # noqa: E731
no_pick = lambda _root, _row: None  # noqa: E731


def report_of(*profiles) -> Slowness:
    return Slowness(since=NOW - 7200, until=NOW, runs=list(profiles), longest=[], by_role=[], by_conversation=[], total=rollup("all", list(profiles)))


def judge(subject: RunRow, peers: int = 4, kit: StartKit | None = None, **other) -> eff.RunEfficiency:
    rows = [subject, *(row(100 + i) for i in range(peers))]
    done = efficiency([subject], rows, Path("."), kit=kits({subject.run_id: kit} if kit else {}), pick=no_pick, **other)
    return done.runs[0]


# --- 1. started with less ------------------------------------------------------------------------------------------------------


def test_a_start_with_far_fewer_tools_mcp_servers_skills_or_no_memory_than_its_role_is_named_with_both_numbers() -> None:
    item = judge(row(1), kit=StartKit(tools=27, mcp=0, skills=40, memory=False))
    assert item.started_with_less == ["tools 27 of 141", "MCP servers 0 of 6", "skills 40 of 60", "no memory notes"]
    assert judge(row(1), kit=StartKit(tools=141, mcp=6, skills=50, memory=True)).started_with_less == [], "a few skills fewer is within the role's normal"


def test_a_role_with_too_few_peers_or_a_start_with_no_init_line_gets_no_verdict_rather_than_a_guess() -> None:
    assert judge(row(1), peers=2, kit=StartKit(tools=1, mcp=0, skills=0, memory=False)).start_unjudged == "2 peers of 3"
    only = efficiency([row(1)], [row(1)], Path("."), kit=lambda *_: None, pick=no_pick)
    assert only.runs[0].start_unjudged == "no start line" and only.runs[0].started_with_less == []


def test_the_init_line_of_a_raw_stream_gives_the_kit_and_another_provider_s_stream_gives_none(tmp_path: Path) -> None:
    init = {"type": "system", "subtype": "init", "tools": ["a"] * 27, "mcp_servers": [{}], "skills": ["s"] * 48, "memory_paths": {"auto": "/x"}}
    (tmp_path / f"{run_id(1)}.raw.jsonl").write_text(json.dumps({"type": "other"}) + "\n" + json.dumps(init) + "\n")
    (tmp_path / f"{run_id(2)}.raw.jsonl").write_text(json.dumps({"type": "thread.started"}) + "\n")
    assert read_start(tmp_path, run_id(1)) == StartKit(tools=27, mcp=1, skills=48, memory=True)
    assert read_start(tmp_path, run_id(2)) is None and read_start(tmp_path, run_id(3)) is None


# --- 2. bookkeeping ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, label", [
    ("command='rg x .github/memory/decisions.md'", "decisions"),
    ("file_path='~/.galaius/out/ledgers/abcdef12.md'", "ledger"),
    ("command='galaius agents list --json'", "agents list"),
    ("command='cd w; deploy_queue.py wait 12'", "deploy wait"),
    ("file_path='~/.galaius/out/ledgers/abcdef12-shot.png'", ""),
    ("command='make test'", ""),
])
def test_a_tool_call_is_bookkeeping_by_what_its_input_names(text: str, label: str) -> None:
    assert bookkeeping_label(text) == label


def test_a_workspace_adds_its_own_bookkeeping_calls_through_the_configuration(monkeypatch) -> None:
    monkeypatch.setenv("GALAIUS_BOOKKEEPING_CALLS", json.dumps({"tracker": r"\btracker\s+add\b", "broken": "("}))
    patterns = run_slowness.bookkeeping_patterns()
    assert bookkeeping_label("command='tracker add x'", patterns) == "tracker" and bookkeeping_label("command='tracker add x'") == ""
    assert bookkeeping_label("command='galaius agents list'", patterns) == "agents list", "the built-in ones stay; a bad expression is skipped"


def _call(i: int, at: float, text: str) -> list[str]:
    return [json.dumps({"kind": "tool", "tool": "Bash", "tool_input": text, "tool_id": f"t{i}", "at": at}, separators=(",", ":")), json.dumps({"kind": "tool_result", "tool_id": f"t{i}", "text": "", "at": at + 1}, separators=(",", ":"))]


def _bookkept(tmp_path: Path, calls: int, bookkeeping: int) -> tuple[RunRow, object]:
    t = NOW - 3000
    lines = [json.dumps({"kind": "started", "at": t}, separators=(",", ":"))]
    for i in range(calls):
        lines += _call(i, t + 10 + i * 5, "command='galaius agents list'" if i < bookkeeping else "command='make test'")
    (tmp_path / f"{run_id(1)}.jsonl").write_text("\n".join(lines) + "\n")
    return row(1), read_transcript(tmp_path / f"{run_id(1)}.jsonl", 0)


def test_a_run_over_a_fifth_of_its_calls_on_bookkeeping_is_a_finding_that_names_the_kinds_and_a_short_run_is_not(tmp_path: Path) -> None:
    subject, transcript = _bookkept(tmp_path, calls=40, bookkeeping=12)
    profile = profile_run(subject, transcript, since=NOW - 3600, now=NOW, queued=None)
    assert (profile.calls, profile.bookkeeping) == (40, {"agents list": 12})
    report = report_of(profile)
    said = findings(report, efficiency([subject], [], Path("."), kit=kits({}), pick=no_pick))
    assert any("30%" in line and "agents list 12" in line and run_id(1)[:8] in line for line in said)
    short, shorter = _bookkept(tmp_path, calls=10, bookkeeping=9)
    quiet = profile_run(short, shorter, since=NOW - 3600, now=NOW, queued=None)
    assert quiet.calls == 10 and not any("bookkeeping" in line for line in findings(report.model_copy(update={"runs": [quiet]}), efficiency([short], [], Path("."), kit=kits({}), pick=no_pick)))


def test_only_the_calls_made_inside_the_window_are_counted_whatever_the_age_of_the_file(tmp_path: Path) -> None:
    """A transcript touched today still holds the run's calls of days ago: the share is read from each call's own time (2026-11-23: counting a
    whole file's history gave 13% where the day's calls held 8.7%)."""
    t = NOW - 10 * 86400
    lines = [json.dumps({"kind": "started", "at": t}, separators=(",", ":"))]
    for i in range(30):   # ten days ago: every call bookkeeping
        lines += _call(i, t + 10 + i * 5, "command='galaius agents list'")
    for i in range(30, 50):   # inside the window: none
        lines += _call(i, NOW - 1800 + i, "command='make test'")
    (tmp_path / f"{run_id(1)}.jsonl").write_text("\n".join(lines) + "\n")
    transcript = read_transcript(tmp_path / f"{run_id(1)}.jsonl", 0)
    subject = row(1).model_copy(update={"started_at": t})
    profile = profile_run(subject, transcript, since=NOW - 3600, now=NOW, queued=None)
    assert (profile.calls, profile.bookkeeping_calls()) == (20, 0)
    assert profile_run(subject, transcript, since=t - 1, now=NOW, queued=None).calls == 50


# --- 3. a wait for a place -----------------------------------------------------------------------------------------------------


def test_a_run_that_waited_over_fifteen_minutes_for_a_place_is_a_finding_and_one_that_waited_five_is_not(tmp_path: Path) -> None:
    subject = row(1)
    long, brief = (profile_run(subject, read_transcript(tmp_path / "none", 0), since=NOW - 7200, now=NOW, queued=wait) for wait in (1200.0, 300.0))
    for profile, found in ((long, True), (brief, False)):
        report = report_of(profile)
        said = findings(report, efficiency([subject], [], Path("."), kit=kits({}), pick=no_pick))
        assert any("waited 20m for an agent place" in line for line in said) is found


def test_a_spawn_still_waiting_for_a_place_over_fifteen_minutes_is_a_doctor_finding(tmp_path: Path) -> None:
    (tmp_path / "#spawn-line.state").write_text(json.dumps([{"run_id": "bbbbbbbb-0", "state": "waiting", "asked_at": NOW - 5 * 3600}, {"run_id": "cccccccc-0", "state": "waiting", "asked_at": NOW - 60}]))
    assert health.agents(NOW, tmp_path, limit=6).findings == ["2 spawns waiting for an agent place, the oldest for 5.0h (6 places)"]


# --- 4. a vendor-error end -----------------------------------------------------------------------------------------------------


def test_a_run_ended_by_a_vendor_error_with_nothing_to_carry_it_on_is_named_and_a_run_that_was_carried_on_or_finished_is_not() -> None:
    overloaded = row(1, status="failed", said="API Error: 529 Overloaded. This is a server-side issue")
    assert vendor_end(overloaded).startswith("API Error: 529")
    assert vendor_end(row(2, status="failed", cut="login_lost", said="Not logged in")) == "Not logged in"
    assert vendor_end(row(3, status="failed", said="API Error: 529 Overloaded", successor="abcd1234")) == "", "a successor carried its work on"
    assert vendor_end(row(4, status="done", said="API Error: 529 mentioned in its report")) == ""
    assert vendor_end(row(5, status="failed", said="a test failed: assertion")) == "" and vendor_end(row(6, status="running", cut="usage_limit")) == ""
    assert judge(overloaded).vendor_end


# --- 5. a model or effort below the role's rule --------------------------------------------------------------------------------


def _record(tmp_path: Path, n: int, *, model: str, effort: str, skipped: list[dict] | None = None) -> None:
    candidates = [{"provider": "claude", "model": name, "rank": i} for i, name in enumerate(["first", "second", "third"])]
    (tmp_path / f"{run_id(n)}.json").write_text(json.dumps({"candidates": candidates, "skipped": skipped or [], "model": model, "reasoning": effort}))


def test_the_rank_of_the_model_and_the_effort_come_from_the_record_and_a_fallback_with_a_recorded_reason_is_routine(tmp_path: Path) -> None:
    _record(tmp_path, 1, model="third", effort="low", skipped=[{"candidate": {"model": "first"}, "reason": "quota_exceeded"}])
    subject = row(1).model_copy(update={"model": "third"})
    assert read_pick(tmp_path, subject) == Pick(rank=2, first="first", why="quota_exceeded", effort="low")
    for n in range(100, 104):
        _record(tmp_path, n, model="first", effort="high")
    peers = [row(100 + i).model_copy(update={"model": "first"}) for i in range(4)]
    item = efficiency([subject], [subject, *peers], tmp_path, kit=kits({}))
    result = item.runs[0]
    assert result.effort_below_role == "effort low, the role's usual is high"
    assert result.model_below_pick.startswith("third is the rule's pick #3, first pick first passed over: quota_exceeded") and result.model_below_explained
    rows_report = report_of(profile_run(subject, read_transcript(tmp_path / "none", 0), since=NOW - 3600, now=NOW, queued=None))
    said = findings(rows_report, item)
    assert any("lower effort" in line for line in said) and not any("first pick" in line for line in said), "an explained fallback shows in `slow`, never as a finding"
    _record(tmp_path, 1, model="third", effort="high")
    unexplained = efficiency([subject], [subject, *peers], tmp_path, kit=kits({})).runs[0]
    assert unexplained.model_below_pick.endswith("(no reason recorded)") and not unexplained.model_below_explained


# --- the whole read, from files, and what the periodic check posts ----------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    folder = tmp_path / "agents"
    folder.mkdir()
    now = time.time()
    for n in range(1, 6):
        (folder / f"{run_id(n)}.json").write_text(json.dumps({"run_id": run_id(n), "agent": "backend-engineer", "status": "done", "provider": "claude", "model": "m",
                                                               "started_at": now - 1800 - n, "finished_at": now - 60 - n, "stream_digest": {"source": "x"}}))
        tools = 27 if n == 1 else 141
        init = {"type": "system", "subtype": "init", "tools": ["t"] * tools, "mcp_servers": [{}] * 6, "skills": ["s"] * 60, "memory_paths": {"auto": "/x"}}
        (folder / f"{run_id(n)}.raw.jsonl").write_text(json.dumps(init) + "\n")
    return folder


def test_the_look_over_a_registry_folder_finds_the_one_run_that_started_with_less(registry: Path) -> None:
    report, found, said = look(RunLedger(registry), now=time.time())
    assert report.total.runs == 5 and [r.run_id for r in found.runs if r.started_with_less] == [run_id(1)]
    assert said == [f"backend-engineer: 1 run ({run_id(1)[:8]}) started with less than the role's normal: tools 27 of 141."]


def test_the_periodic_check_posts_what_broke_once_per_window_as_a_note_that_is_no_one_s_words_and_posts_nothing_when_nothing_broke(registry: Path, monkeypatch, capsys) -> None:
    sent: list[dict] = []

    class Delivered:
        state, text = "queued", "Queued."

    def deliver(target, body, **kw):
        sent.append({"target": target, "body": body, **kw})
        return Delivered()

    monkeypatch.setattr(cli, "RunLedger", lambda *a, **k: RunLedger(registry))
    monkeypatch.setattr(cli.messaging, "deliver_message", deliver)
    cli.check(post=True, to=run_id(9))
    assert len(sent) == 1 and sent[0]["target"] == run_id(9) and sent[0]["sender"] == cli.CHECK_SENDER and sent[0]["turn"].fyi
    assert "tools 27 of 141" in sent[0]["body"] and sent[0]["message_id"].startswith("efficiency-check-")
    (registry / f"{run_id(1)}.raw.jsonl").write_text(json.dumps({"type": "system", "subtype": "init", "tools": ["t"] * 141, "mcp_servers": [{}] * 6, "skills": ["s"] * 60, "memory_paths": {"a": "/x"}}) + "\n")
    cli.check(post=True, to=run_id(9))
    assert len(sent) == 1, "nothing broke, nothing is sent"
    assert "0 findings" in capsys.readouterr().out


def test_the_doctor_area_carries_the_counts_and_the_findings(registry: Path, monkeypatch) -> None:
    monkeypatch.setattr("galaius.agents.run_ledger.UserPaths.agents", classmethod(lambda cls: registry), raising=False)
    check = health.efficiency(time.time())
    assert check.area == "efficiency" and check.facts["runs"] == 5 and check.facts["started_with_less"] == 1 and len(check.findings) == 1
