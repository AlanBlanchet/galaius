"""macOS keeps a computer connected with a launchd agent of its user (`LaunchdMachineService`):
loaded at their login, restarted after a crash, never after a clean exit (a revoked computer)."""

import plistlib
import subprocess
from pathlib import Path

import pytest

from galaius.machine_service import LaunchdMachineService, ServiceUnavailable


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    calls: list[list[str]] = []
    loaded = {"yes": False}

    def run(argv, **_options):
        calls.append(argv[1:])
        verb = argv[1]
        loaded["yes"] = {"bootstrap": True, "bootout": False}.get(verb, loaded["yes"])
        code = 0 if verb != "print" or loaded["yes"] else 113
        return subprocess.CompletedProcess(argv, code, "state = running\n" if code == 0 and verb == "print" else "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_the_agent_starts_at_login_and_restarts_after_a_crash(agent: list[list[str]], tmp_path: Path) -> None:
    service = LaunchdMachineService()
    service.install()
    definition = plistlib.loads(service.path.read_bytes())
    assert service.path == tmp_path / "Library" / "LaunchAgents" / "ai.galaius.machine.plist"
    assert definition["Label"] == "ai.galaius.machine" and definition["ProgramArguments"][1:] == ["machine", "connect"]
    assert Path(definition["ProgramArguments"][0]).name == "galaius"
    assert definition["RunAtLoad"] is True and definition["KeepAlive"] == {"SuccessfulExit": False}
    assert definition["StandardErrorPath"] == str(tmp_path / ".config" / "galaius" / "machine-service.log")
    assert agent == [["bootout", "gui/501/ai.galaius.machine"], ["bootstrap", "gui/501", str(service.path)]]
    assert service.running() and service.installed()
    service.stop()
    assert not service.running()
    service.start()  # unloaded by stop: loaded again, then started
    assert agent[-2:] == [["bootstrap", "gui/501", str(service.path)], ["kickstart", "gui/501/ai.galaius.machine"]]
    service.remove()
    assert not service.installed()
    with pytest.raises(ServiceUnavailable, match="galaius login"):
        service.start()
