"""An agent program installed from the web runs only its vendor's own script: fetched over https,
through its vendor's hosts alone, never larger than a script."""

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
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider: (False, ""))
    monkeypatch.setattr(AgentPrograms, "installable", staticmethod(lambda: (PROVIDERS["claude"],)))
    monkeypatch.setattr(AgentPrograms, "version", lambda self, provider: (_ for _ in ()).throw(Broken("no version")))
    monkeypatch.setattr(PROVIDERS["claude"], "available", lambda: False)
    started = threading.Event()

    def install(self, provider):
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
    jobs._update("claude", only_while="signing_in", sign_in=AgentSignIn(code="ABCD-12345", expires_at=datetime.now(UTC)))
    assert {item.provider: item for item in jobs.states()}["claude"].sign_in is None
