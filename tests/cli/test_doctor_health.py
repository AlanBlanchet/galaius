"""`galaius doctor`'s health look (`galaius.diagnostics.health`): each area answers from a fake program or folder, a finding names its evidence, an
area that cannot be looked at says why, and the whole look never waits past its bound. No real systemd, journal, network or `gh` is touched."""

import json
import sys
import time
from pathlib import Path

import httpx
import pytest

from galaius.cli.commands.debug import doctor as doctor_cli
from galaius.diagnostics import health
from galaius.diagnostics.health import Check, Health, Unavailable
from galaius.machine.presence import Presence, PresenceFiles

linux = pytest.mark.skipif(sys.platform != "linux", reason="the journal and user units are systemd's")
NOW = 1_800_000_000.0


def journal(*lines: tuple[float, str]) -> str:
    """What `journalctl -o short-unix` prints for the machine service: a rich line (no JSON) and the structured line of each entry."""
    out = []
    for at, said in lines:
        out.append(f"{at:.6f} host galaius[1]: [11/23/26 00:39:14] INFO     {said[:20]}   runner.py:233")
        out.append(f"{at:.6f} host galaius[1]: " + json.dumps({"ts": "x", "level": "info", "message": said, "machine_id": "m"}))
    return "\n".join(out)


def fake(answers: dict[str, str | Exception]):
    """A `Run` answering by the program's first word (+ the subcommand for systemctl / journalctl)."""
    def run(argv: list[str], timeout: float, ok: tuple[int, ...]) -> str:
        key = next((key for key in answers if key in " ".join(argv)), None)
        if key is None:
            raise Unavailable(f"{argv[0]} is not installed")
        answer = answers[key]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return run


# --- the link ----------------------------------------------------------------------------------------------------------


@linux
def test_a_link_with_no_connection_process_is_a_finding_that_names_the_last_trouble_and_the_last_hello(tmp_path: Path) -> None:
    files = PresenceFiles(folder=tmp_path)
    files.write(Presence(link="connected", pc="box", version="1.2.3", runs=2))   # written, but nothing holds the lock: the process is gone
    story = journal((NOW - 3600, "connected to wss://app/v1/machine-channel"), (NOW - 600, "server unreachable: ConnectError"))
    check = health.link(NOW, fake({"journalctl": story}), tmp_path)
    assert check.facts["shown"] == "off" and check.facts["last_hello_ago"] == "60m" and check.facts["last_trouble_ago"] == "10m"
    assert len(check.findings) == 1
    assert "no connection process" in check.findings[0] and "server unreachable" in check.findings[0]


@linux
def test_a_link_held_and_connected_is_no_finding_and_a_refused_one_says_to_sign_in_again(tmp_path: Path) -> None:
    files = PresenceFiles(folder=tmp_path)
    files.write(Presence(link="connected", pc="box"))
    held = files.hold()
    assert held is not None
    quiet = fake({"journalctl": journal((NOW - 60, "connected to wss://app/v1/machine-channel"))})
    assert health.link(NOW, quiet, tmp_path).findings == []
    files.write(Presence(link="refused"))
    assert "galaius login" in health.link(NOW, quiet, tmp_path).findings[0]
    files.write(Presence(link="reconnecting"))
    assert health.link(NOW, quiet, tmp_path).findings[0].startswith("reconnecting, last hello 60s ago")


@linux
def test_an_unreadable_journal_is_said_not_taken_for_a_good_link(tmp_path: Path) -> None:
    PresenceFiles(folder=tmp_path).write(Presence(link="connected"))
    check = health.link(NOW, fake({"journalctl": Unavailable("journalctl did not answer within 5 s")}), tmp_path)
    assert "did not answer" in check.skipped and check.facts["last_hello"] == "-"


def test_the_journal_story_keeps_only_each_entrys_message() -> None:
    story = health.journal_story("u", fake({"journalctl": journal((10.0, "connected to x"), (20.0, "server refused the connection: HTTP 502"))}))
    assert story == [(10.0, "connected to x"), (20.0, "server refused the connection: HTTP 502")]


@linux
def test_the_last_hello_is_the_newest_whatever_order_journalctl_prints(tmp_path: Path) -> None:
    """2026-11-23: with `-g` and `-n` the journal printed the newest entry first, and the link line read a hello from 2 h before the real one."""
    files = PresenceFiles(folder=tmp_path)
    files.write(Presence(link="reconnecting"))
    held = files.hold()
    assert held is not None
    entries = [(NOW - 7200, "connected to wss://app/v1/machine-channel"), (NOW - 7190, "server refused the connection: HTTP 502"), (NOW - 120, "connection closed (code 1012)"),
               (NOW - 117, "connected to wss://app/v1/machine-channel")]
    for ordered in (entries, entries[::-1]):
        check = health.link(NOW, fake({"journalctl": journal(*ordered)}), tmp_path)
        assert check.facts["last_hello_ago"] == "1m" or check.facts["last_hello_ago"] == "2m", check.facts
        assert check.facts["last_trouble_said"] == "connection closed (code 1012)"
        assert check.findings[0].startswith("reconnecting, last hello ") and "2h" not in check.findings[0]


# --- the app -----------------------------------------------------------------------------------------------------------


def machine_file(folder: Path, server: str = "https://app.test") -> None:
    (folder / "machine.json").write_text(json.dumps({"server_url": server, "token": "never-read"}))


def answer(status: int = 200, body: dict | None = None):
    return lambda url, timeout: httpx.Response(status, json=body if body is not None else {}, request=httpx.Request("GET", url))


def test_the_app_check_reads_the_build_and_flags_a_core_skew(tmp_path: Path, monkeypatch) -> None:
    machine_file(tmp_path)
    monkeypatch.setattr(health, "_core_version", lambda: "1.0.0")
    good = health.app(NOW, tmp_path, answer(200, {"build_id": "2026-abc1234", "version": "0.2", "core_version": "1.0.0"}))
    assert good.findings == [] and good.facts["build_id"] == "2026-abc1234" and good.facts["server"] == "https://app.test"
    skewed = health.app(NOW, tmp_path, answer(200, {"core_version": "2.0.0"}))
    assert "galaius-core 1.0.0, the app 2.0.0" in skewed.findings[0]


def test_an_app_that_errors_or_does_not_answer_is_a_finding_with_the_status_or_the_reason(tmp_path: Path) -> None:
    machine_file(tmp_path)
    assert "HTTP 503" in health.app(NOW, tmp_path, answer(503)).findings[0]

    def down(url: str, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    assert "does not answer: connection refused" in health.app(NOW, tmp_path, down).findings[0]


def test_no_sign_in_skips_the_app_check_and_never_reads_the_token(tmp_path: Path) -> None:
    assert "galaius login" in health.app(NOW, tmp_path).skipped
    machine_file(tmp_path)
    check = health.app(NOW, tmp_path, answer(200, {}))
    assert "never-read" not in check.model_dump_json()


# --- units, agents, limits, ci -----------------------------------------------------------------------------------------


SHOW = """Id=galaius-run-mirror.service
ActiveState=activating
SubState=auto-restart
Result=exit-code
NRestarts=5575
ExecMainStatus=1

Id=galaius-test-pc.service
ActiveState=failed
SubState=failed
Result=exit-code
NRestarts=0
ExecMainStatus=3

Id=galaius-machine.service
ActiveState=active
SubState=running
Result=success
NRestarts=0
ExecMainStatus=0

Id=galaius-pc-follow.service
ActiveState=inactive
SubState=dead
Result=success
NRestarts=0
ExecMainStatus=0
"""


@linux
def test_a_failed_unit_and_a_restart_loop_are_findings_with_the_units_own_last_line_and_a_resting_one_is_nothing() -> None:
    lines = "Traceback (most recent call last):\nImportError: cannot import name 'X' token=abcdefghijklmnop\ngalaius-run-mirror.service: Consumed 3s CPU time.\nStarted galaius-run-mirror.service - x"
    check = health.units(fake({"show": SHOW, "-u": lines}))
    said = " | ".join(check.findings)
    assert len(check.findings) == 2
    assert "galaius-run-mirror.service restart loop" in said and "5575 restarts" in said and "ImportError: cannot import name 'X'" in said
    assert "galaius-test-pc.service failed (result exit-code" in said and "exit status 3" in said
    assert "abcdefghijklmnop" not in said, "a credential shape in a journal line is masked"
    assert "pc-follow" in check.facts.keys().__str__() and "machine" in check.facts.keys().__str__()


def unit(name: str, active: str, sub: str, restarts: int, entered_s: float) -> str:
    return f"Id={name}\nActiveState={active}\nSubState={sub}\nResult=success\nNRestarts={restarts}\nExecMainStatus=1\nActiveEnterTimestampMonotonic={int(entered_s * 1e6)}\n"


@linux
def test_a_restart_loop_is_read_from_the_counter_not_from_the_state_at_one_instant() -> None:
    """The run mirror crashes every ~6 s: looked at in its up phase it is `active (running)` (tester 3c84ef7f saw 1 finding, then 2)."""
    shown = "\n".join([unit("galaius-up-phase.service", "active", "running", 5871, 996.0),      # up 4 s after thousands of restarts: a loop
                       unit("galaius-old-hand.service", "active", "running", 5, 1.0),            # restarted 5 times, up for 16 min: steady
                       unit("galaius-once.service", "active", "running", 1, 999.0),              # one restart, up 1 s: not a loop yet
                       unit("galaius-timer-job.service", "activating", "start", 0, 0.0)])        # a one-shot starting for the first time: nothing
    check = health.units(fake({"show": shown, "-u": "ValueError: why\n"}), monotonic=1000.0)
    assert [finding.split(" restart loop")[0] for finding in check.findings] == ["galaius-up-phase.service"]
    assert "up 4s" in check.findings[0] and "5871 restarts" in check.findings[0]


@linux
def test_a_missing_systemctl_skips_units_with_the_reason() -> None:
    assert "not installed" in health.units(fake({})).skipped


def test_a_failed_spawn_in_the_line_is_a_finding_and_waiting_ones_are_counted(tmp_path: Path) -> None:
    (tmp_path / "#spawn-line.state").write_text(json.dumps([
        {"run_id": "aaaaaaaa-0", "state": "failed", "said": "its start was cut off; spawn it again", "asked_at": NOW - 100},
        {"run_id": "bbbbbbbb-0", "state": "waiting", "asked_at": NOW - 300},
        {"run_id": "cccccccc-0", "state": "waiting", "asked_at": NOW - 60},
        "not an entry"]))
    check = health.agents(NOW, tmp_path, limit=6)
    assert check.facts["places_max"] == 6 and check.facts["waiting"] == 2 and check.facts["oldest_wait"] == "5m"
    assert check.findings == ["spawn aaaaaaaa will not start: its start was cut off; spawn it again"]


def test_a_missing_or_garbled_spawn_line_is_no_failure_and_a_garbled_one_is_said(tmp_path: Path) -> None:
    assert health.agents(NOW, tmp_path, limit=6).findings == []
    (tmp_path / "#spawn-line.state").write_text("{not json")
    assert "unreadable" in health.agents(NOW, tmp_path, limit=6).skipped


CI_RUNS = [{"workflowName": "ci", "status": "completed", "conclusion": "failure", "headSha": "abcdef1234", "createdAt": "2026-11-22T21:37:12Z", "url": "https://x/1"},
           {"workflowName": "windows", "status": "completed", "conclusion": "success", "headSha": "abcdef1234", "createdAt": "2026-11-22T21:37:12Z", "url": "https://x/2"},
           {"workflowName": "ci", "status": "completed", "conclusion": "success", "headSha": "0000000", "createdAt": "2026-11-22T10:00:00Z", "url": "https://x/3"}]


def test_ci_judges_the_newest_run_of_each_workflow_and_a_cancelled_run_is_no_failure() -> None:
    check = health.ci(time.time(), fake({"gh": json.dumps(CI_RUNS)}), slug="o/r")
    assert len(check.findings) == 1 and "CI ci on main is failure at abcdef1" in check.findings[0] and "https://x/1" in check.findings[0]
    cancelled = [{**CI_RUNS[0], "conclusion": "cancelled"}]
    assert health.ci(time.time(), fake({"gh": json.dumps(cancelled)}), slug="o/r").findings == []


def test_ci_with_no_gh_or_no_repository_is_skipped_with_the_reason() -> None:
    assert "not installed" in health.ci(NOW, fake({}), slug="o/r").skipped
    assert "gh auth" in health.ci(NOW, fake({"gh": Unavailable("run gh auth login")}), slug="o/r").skipped


# --- the whole look ----------------------------------------------------------------------------------------------------


def test_the_look_bounds_a_slow_area_and_turns_a_crashed_one_into_a_finding(monkeypatch) -> None:
    quiet = lambda *_a, **_k: Check(area="x")  # noqa: E731
    for name in ("app", "units", "agents", "ci"):
        monkeypatch.setattr(health, name, quiet)
    monkeypatch.setattr(health, "limits", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(health, "link", lambda *_a: time.sleep(2) or Check(area="link"))
    monkeypatch.setattr(health, "LOOK_SECONDS", 0.2)
    started = time.monotonic()
    report = health.look(fake({}))
    assert time.monotonic() - started < 1.5, "a slow area is left behind, not waited for"
    assert "no answer within 0.2 s" in report.skipped["link"]
    assert any(finding.area == "limits" and "RuntimeError: boom" in finding.said for finding in report.findings)


def test_the_exit_code_is_the_finding_count_up_to_255() -> None:
    many = Health.of([Check(area="a", findings=["x"] * 300)], 0.1)
    assert many.exit_code() == 255 and Health.of([Check(area="a")], 0.1).exit_code() == 0


def test_the_command_prints_findings_first_and_exits_with_their_count(monkeypatch, capsys) -> None:
    report = Health.of([Check(area="link", facts={"shown": "off"}, findings=["no connection process"]), Check(area="ci", skipped="gh is not installed")], 0.5)
    monkeypatch.setattr(health, "look", lambda: report)
    with pytest.raises(SystemExit) as stopped:
        doctor_cli.doctor()
    out = capsys.readouterr().out.splitlines()
    assert stopped.value.code == 1 and out[0].startswith("galaius doctor · 1 finding") and out[1].strip() == "✗ link: no connection process"
    assert any("ci" in line and "not looked at: gh is not installed" in line for line in out)
    with pytest.raises(SystemExit):
        doctor_cli.doctor(json_out=True)
    shown = json.loads(capsys.readouterr().out)
    assert shown["findings"] == [{"area": "link", "said": "no connection process"}] and shown["skipped"] == {"ci": "gh is not installed"}


def test_a_healthy_look_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(health, "look", lambda: Health.of([Check(area="link", facts={"shown": "connected"})], 0.1))
    with pytest.raises(SystemExit) as stopped:
        doctor_cli.doctor()
    assert stopped.value.code == 0
    assert "0 findings" in capsys.readouterr().out
