"""The agent fence (`interact.fence.Fence`): an agent CLI started from this PC's levels sees only
the folders set to read or later (read-only below `write_on_review`, its staging copy for that
level), its own tool state, and the system; never the rest of the home folder, the desktop's
sockets or the user's session bus. Live checks run where this PC can build the fence (Linux with
bubblewrap and Landlock scopes); elsewhere the fence says why it is unavailable."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from interact.fence import Fence, available
from interact.places import PlaceMap


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    for folder in ("docs", "work", "review", "private", ".ssh", ".claude", "interact-files"):
        (home / folder).mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (home / "private" / "diary.txt").write_text("dear diary")
    (home / "docs" / "plan.txt").write_text("the plan")
    (home / "review" / "draft.txt").write_text("draft")
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    return home


def _fence(home: Path, staging: Path | None = None) -> Fence:
    places = PlaceMap(working_directory=home, levels={"docs": "read", "work": "write", "review": "write_on_review", "private": "see"})
    return Fence.build(places, start=home / "work", staging={"review": staging} if staging else {}, tool_state=(home / ".claude",))


def test_binds_follow_the_levels_and_nothing_else_of_home(home: Path, tmp_path: Path) -> None:
    staging = tmp_path / "staging-review"
    staging.mkdir()
    fence = _fence(home, staging)
    binds = {(bind.source, bind.target, bind.writable) for bind in fence.binds}
    assert (home / "docs", home / "docs", False) in binds
    assert (home / "work", home / "work", True) in binds
    assert (staging, home / "review", True) in binds  # writes land in the staging copy, never the folder
    assert not any(bind.target in {home / "private", home / ".ssh", home} for bind in fence.binds)  # see / hidden / home itself
    assert (home / ".claude" / "settings.json", home / ".claude" / "settings.json", False) in binds  # the tool's own settings stay read-only


def test_a_write_on_review_folder_without_its_staging_copy_is_refused(home: Path) -> None:
    places = PlaceMap(working_directory=home, levels={"review": "write_on_review"})
    with pytest.raises(PermissionError, match="staging copy"):
        Fence.build(places, start=home / "review", staging={}, tool_state=())


def test_the_start_folder_must_be_open_to_agents(home: Path) -> None:
    with pytest.raises(PermissionError, match="no level"):
        Fence.build(PlaceMap(working_directory=home, levels={"docs": "read"}), start=home / "private", staging={}, tool_state=())


def test_the_desktop_and_session_sockets_are_unset_inside(home: Path) -> None:
    command = Fence.build(PlaceMap(working_directory=home, levels={"work": "write"}), start=home / "work", staging={}, tool_state=()).command(["true"])
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR"):
        assert command[command.index(name) - 1] == "--unsetenv"
    assert command[:3] == [sys.executable, "-m", "interact.fence"]


def test_off_linux_the_fence_says_why_it_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    available.cache_clear()
    monkeypatch.setattr(sys, "platform", "darwin")
    ok, reason = available()
    available.cache_clear()
    assert not ok and "Linux" in reason


live = pytest.mark.skipif(not available()[0], reason=f"no fence on this PC: {available()[1]}")


def _inside(fence: Fence, script: str) -> dict:
    done = subprocess.run(fence.command([sys.executable, "-c", script]), cwd=fence.cwd, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "DISPLAY": ":0"})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


PROBE = r"""
import json, os, socket
def attempt(action):
    try:
        action(); return "ok"
    except OSError as error:
        return type(error).__name__
home = os.environ["HOME"]
def x11():
    s = socket.socket(socket.AF_UNIX); s.connect("\0/tmp/.X11-unix/X0")
print(json.dumps({
    "hidden": attempt(lambda: open(os.path.join(home, ".ssh", "id_ed25519")).read()),
    "see_level": attempt(lambda: open(os.path.join(home, "private", "diary.txt")).read()),
    "read": attempt(lambda: open(os.path.join(home, "docs", "plan.txt")).read()),
    "read_write": attempt(lambda: open(os.path.join(home, "docs", "new.txt"), "w").write("x")),
    "write": attempt(lambda: open(os.path.join(home, "work", "out.txt"), "w").write("done")),
    "review_write": attempt(lambda: open(os.path.join(home, "review", "draft.txt"), "w").write("edited")),
    "tool_settings_write": attempt(lambda: open(os.path.join(home, ".claude", "settings.json"), "w").write("{\"hooks\":1}")),
    "home": sorted(os.listdir(home)),
    "bus": os.path.exists("/run/user/%d/bus" % os.getuid()),
    "x11": attempt(x11),
}))
"""


@live
def test_a_fenced_process_reads_and_writes_only_what_the_levels_open(home: Path, tmp_path: Path) -> None:
    staging = tmp_path / "staging-review"
    staging.mkdir()
    (staging / "draft.txt").write_text("draft")
    seen = _inside(_fence(home, staging), PROBE)
    assert seen["hidden"] != "ok" and seen["see_level"] != "ok"  # M3: a hidden file is not readable under the fence
    assert seen["read"] == "ok" and seen["read_write"] != "ok"
    assert seen["write"] == "ok" and (home / "work" / "out.txt").read_text() == "done"
    assert seen["review_write"] == "ok" and (home / "review" / "draft.txt").read_text() == "draft" and (staging / "draft.txt").read_text() == "edited"
    assert seen["tool_settings_write"] != "ok"
    assert set(seen["home"]) <= {"docs", "work", "review", ".claude"}
    assert seen["bus"] is False and seen["x11"] != "ok"


@pytest.mark.asyncio
async def test_a_fenced_launch_starts_inside_the_fence_and_its_run_keeps_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher wraps the CLI in the fence, and the run's record keeps it for every later turn
    (the chosen candidate is registered twice: the second write must not drop it)."""
    import asyncio

    from interact.agents import registry as reg
    from interact.agents import run as run_module
    from tests.support.agents import install_provider, use_policy
    from tests.test_agent_run import _FakeProvider

    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    install_provider(monkeypatch, _FakeProvider())
    started: list[list[str]] = []
    monkeypatch.setattr(run_module, "contained", lambda argv: started.append(list(argv)) or [sys.executable, "-c", "pass"])
    (tmp_path / "work").mkdir()
    fence = Fence.build(PlaceMap(working_directory=tmp_path, levels={"work": "write"}), start=tmp_path / "work", staging={}, tool_state=())
    run = await run_module.run_agent(_FakeProvider(), "t", agent="tester", name="w", cwd=str(tmp_path / "work"), fence=fence)
    await asyncio.wait_for(run.wait(), timeout=30)
    assert started[0][:3] == [sys.executable, "-m", "interact.fence"]
    assert reg.get_run(run.run_id).fence == fence
