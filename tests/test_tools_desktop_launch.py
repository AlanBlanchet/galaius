"""launch_app command handling at the tool level: a shell-syntax command (`cd X && app`) runs via
bash instead of failing exec with `[Errno 2] No such file or directory: 'cd'`, and `cwd=` starts
the app in a project directory directly — the two launch shapes real sessions actually used."""

import asyncio

import pytest

import interact.server as srv


class _FakeBackend:
    display = ":88"

    def __init__(self):
        self.spawned: list[tuple[list[str], str | None]] = []

    def spawn(self, argv, cwd=None):
        self.spawned.append((list(argv), cwd))
        return type("P", (), {"poll": lambda self: None, "returncode": None})()

    def list_windows(self):
        return [(7, "App")]


@pytest.fixture
def fake_backend(monkeypatch):
    fb = _FakeBackend()
    monkeypatch.setattr(srv.sandbox, "_get_sandbox", lambda size=None: fb)
    monkeypatch.setattr(srv.targets, "_desktop_unsupported", lambda: None)
    return fb


def test_cd_command_runs_via_shell(fake_backend):
    cmd = "cd ~/proj && exec uv run app"
    out = asyncio.run(srv.launch_app(cmd, wait=1))
    assert "ERROR" not in out and "No such file" not in out
    argv, _ = fake_backend.spawned[0]
    assert argv[:2] == ["bash", "-c"] and argv[2] == cmd


def test_cwd_param_spawns_in_that_directory(fake_backend, tmp_path):
    out = asyncio.run(srv.launch_app("uv run app", wait=1, cwd=str(tmp_path)))
    assert "ERROR" not in out
    argv, cwd = fake_backend.spawned[0]
    assert argv == ["uv", "run", "app"] and cwd == str(tmp_path)


def test_missing_cwd_is_a_clear_error(fake_backend, tmp_path):
    out = asyncio.run(srv.launch_app("app", cwd=str(tmp_path / "nope")))
    assert out.startswith("ERROR") and "cwd" in out
    assert fake_backend.spawned == []  # never spawned into a wrong directory
