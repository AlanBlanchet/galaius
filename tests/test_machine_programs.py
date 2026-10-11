"""An agent program installed from the web runs only its vendor's own script: fetched over https,
through its vendor's hosts alone, never larger than a script."""

import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from galaius_core import AgentSignIn

from galaius.agents.providers import PROVIDERS
from galaius.machine_programs import AgentPrograms, ProgramFailed

SCRIPT = b"#!/bin/sh\necho installed\n"


def programs(tmp_path: Path, answer) -> AgentPrograms:
    return AgentPrograms(environment={}, log_path=tmp_path / "agent-programs.log", transport=httpx.MockTransport(answer))


@pytest.mark.parametrize(("hops", "fetched"), [
    ({"https://claude.ai/install.sh": "https://downloads.claude.ai/claude-code-releases/bootstrap.sh"}, True),
    ({"https://claude.ai/install.sh": "https://downloads.example.net/bootstrap.sh"}, False),
    ({"https://claude.ai/install.sh": "http://downloads.claude.ai/bootstrap.sh"}, False),
    ({"https://claude.ai/install.sh": "https://downloads.claude.ai:8443/bootstrap.sh"}, False),
])
def test_the_install_script_comes_only_from_its_vendor(tmp_path: Path, hops: dict[str, str], fetched: bool) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(302, headers={"location": hops[url]}) if url in hops else httpx.Response(200, content=SCRIPT)

    installer = PROVIDERS["claude"].installers["posix"]
    if fetched:
        assert programs(tmp_path, answer).download(installer) == SCRIPT
    else:
        with pytest.raises(ProgramFailed, match="left its vendor") as refused:
            programs(tmp_path, answer).download(installer)
        assert refused.value.code == "download_failed"


def test_an_answer_larger_than_any_script_is_refused(tmp_path: Path) -> None:
    huge = programs(tmp_path, lambda request: httpx.Response(200, content=b"x" * (AgentPrograms.script_bytes + 1)))
    with pytest.raises(ProgramFailed, match="larger"):
        huge.download(PROVIDERS["codex"].installers["posix"])


class Broken(Exception):
    pass


def test_a_job_that_breaks_ends_failed_and_a_late_code_never_reopens_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever stops a job (here its version probe and its install both break), its program reads
    `failed`, never stuck `installing`; a device code its sign-in prints afterwards changes nothing,
    and asking again starts a new job."""
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: (False, "", ""))
    monkeypatch.setattr(AgentPrograms, "installable", staticmethod(lambda: (PROVIDERS["claude"],)))
    monkeypatch.setattr(AgentPrograms, "version", lambda self, provider: (_ for _ in ()).throw(Broken("no version")))
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: False)
    started = threading.Event()

    def install(self, provider, key):
        started.wait(5)
        raise Broken("the disk is gone")

    monkeypatch.setattr(AgentPrograms, "_install", install)
    jobs = programs(tmp_path, lambda request: httpx.Response(404))
    assert jobs.install("claude").step == "installing"
    assert jobs.install("claude").step == "installing"  # asked again while it runs: the same job
    started.set()
    deadline = time.monotonic() + 5
    while (state := {item.provider: item for item in jobs.states()}["claude"]).step == "installing" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (state.step, state.failure, state.detail) == ("failed", "install_failed", "Broken: the disk is gone")
    jobs._update(("claude", None), only_while="signing_in", sign_in=AgentSignIn(code="ABCD-12345", expires_at=datetime.now(UTC)))
    assert {item.provider: item for item in jobs.states()}["claude"].sign_in is None


CLAUDE_SIGN_IN = "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=http%3A%2F%2Flocalhost%3A43117%2Fcallback&state=s7"


@pytest.mark.skipif(sys.platform == "win32", reason="its stand-in programs are POSIX shell scripts")
@pytest.mark.parametrize("opens", [True, False])
def test_the_address_claude_opens_reaches_the_job_through_its_browser_never_a_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, opens: bool) -> None:
    """Claude Code opens its sign-in through `BROWSER`: the login runs with galaius's helper there, the
    address it hands over lands on the job's state (with the person's own opener still run), and
    no log carries it; the opener runs with the person's `BROWSER` back, never the helper. A program that never calls `BROWSER` gets its terminal command offered instead."""
    claude = tmp_path / "claude"
    claude.write_text('#!/bin/sh\n[ "$1" = auth ] || exit 0\n' + ('"$BROWSER" "' + CLAUDE_SIGN_IN + '"\n' if opens else "") + "sleep 30\n")
    claude.chmod(0o755)
    opened = tmp_path / "opened"
    opener = tmp_path / "opener"
    opener.write_text(f'#!/bin/sh\necho "$1 $BROWSER $GALAIUS_SIGN_IN_HANDOFF" > {opened}\n')
    opener.chmod(0o755)
    signed_in = threading.Event()
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: (signed_in.is_set(), "", ""))
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.1)
    monkeypatch.setattr(AgentPrograms, "terminal_seconds", 0.5)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: True)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "executable", lambda self: str(claude))
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin", "BROWSER": str(opener)}, log_path=tmp_path / "agent-programs.log")
    assert jobs.install("claude").step == "signing_in"
    deadline = time.monotonic() + 10
    while (state := jobs.states()[0]).sign_in is None or (state.sign_in.link or state.sign_in.command) is None:
        assert time.monotonic() < deadline, "the sign-in never said where to sign in"
        time.sleep(0.05)
    if opens:
        assert (state.sign_in.link, state.sign_in.command) == (CLAUDE_SIGN_IN, None)
    else:
        assert state.sign_in.link is None and state.sign_in.command is not None and state.sign_in.command.endswith("claude auth login")
    signed_in.set()
    while jobs.states()[0].step != "ready":
        assert time.monotonic() < deadline + 5
        time.sleep(0.05)
    if opens:
        assert opened.read_text().split() == [CLAUDE_SIGN_IN, str(opener)]
    assert "43117" not in caplog.text
    assert not jobs.log_path.exists() or "43117" not in jobs.log_path.read_text()
