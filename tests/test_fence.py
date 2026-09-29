"""The agent fence (`interact.fence`): an agent CLI started from this PC's levels sees only the
folders set to read or later, gets a private copy of its tool state, reaches only its model API
through the runner's egress proxy, and cannot leave anything that runs later outside the fence
(its run record, the owner's tool config, git hooks, editor / Claude settings). Every level is
read again at each turn. Live checks run where this PC can build the fence (Linux with
bubblewrap and Landlock scopes); elsewhere the fence says why it is unavailable."""

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from interact.fence import EgressProxy, FenceSpec, available

AGENT_HOSTS = ("api.anthropic.com",)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    files = {
        ".ssh/id_ed25519": "PRIVATE KEY",
        "private/diary.txt": "dear diary",
        "docs/plan.txt": "the plan",
        "docs/id_ed25519": "KEY IN AN OPEN FOLDER",
        "docs/keys/server.pem": "PEM",
        "docs/.env": "TOKEN=1",
        "review/draft.txt": "draft",
        "work/notes.txt": "notes",
        "work/app/src/main.py": "print(1)\n",
        "work/app/.git/hooks/pre-commit.sample": "#!/bin/sh\n",
        "work/app/.git/config": "[core]\n",
        "work/app/.git/HEAD": "ref: refs/heads/main\n",
        "work/app/CLAUDE.md": "the owner's instructions",
        "sandbox/app/.git/hooks/pre-commit.sample": "#!/bin/sh\n",
        "sandbox/app/.git/config": "[core]\n",
        ".claude/settings.json": "{}",
        ".claude/.credentials.json": "{\"token\": 1}",
        ".claude/bin/claude-watch": "#!/bin/sh\n",
        ".claude/ide/50442.lock": "{\"authToken\": \"secret\"}",
        ".claude/projects/other-session/log.jsonl": "someone else's transcript",
        ".claude.json": "{\"mcpServers\": {}}",
        ".interact/out/agents/run-1.json": "{\"fence\": \"recorded\"}",
        ".interact/config.env": "OPENAI_API_KEY=x",
    }
    for name, content in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(content)
    monkeypatch.setenv("HOME", str(home))
    return home


LEVELS = {"docs": "read", "work": "write", "sandbox": "sandbox", "review": "write_on_review", "private": "see"}


def _spec(home: Path, tmp_path: Path, start: str = "work", levels: dict | None = None, **fields) -> FenceSpec:
    staging = tmp_path / "staging-review"
    staging.mkdir(exist_ok=True)
    (staging / "draft.txt").write_text("draft")
    return FenceSpec(working_directory=home, levels=LEVELS if levels is None else levels, start=home / start, staging={"review": staging},
                     providers=("claude",), state=tmp_path / "state", egress=AGENT_HOSTS, reviews=tmp_path / "reviews", **fields)


def test_binds_follow_the_levels_and_nothing_else_of_home(home: Path, tmp_path: Path) -> None:
    fence = _spec(home, tmp_path).build()
    binds = {(bind.source, bind.target, bind.writable) for bind in fence.binds}
    assert (home / "docs", home / "docs", False) in binds
    assert (tmp_path / "staging-review", home / "review", True) in binds  # writes land in the staging copy, never the folder
    assert not any(bind.target in {home / "private", home / ".ssh", home, home / ".interact"} for bind in fence.binds)


def test_a_write_on_review_folder_without_its_staging_copy_is_refused(home: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="staging copy"):
        FenceSpec(working_directory=home, levels={"review": "write_on_review"}, start=home / "review", state=tmp_path / "state").build()


def test_the_start_folder_must_have_a_level_the_owner_set(home: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="no level"):
        _spec(home, tmp_path, start="private").build()


def test_the_desktop_and_session_sockets_are_unset_inside(home: Path, tmp_path: Path) -> None:
    command = _spec(home, tmp_path).build().command(["true"])
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR"):
        assert command[command.index(name) - 1] == "--unsetenv"
    assert command[:4] == [sys.executable, "-m", "interact.fence", "outer"] and "--unshare-net" in command


def test_each_turn_reads_the_levels_as_they_are_now(home: Path, tmp_path: Path) -> None:
    """A narrowing reaches the next turn of a run started before it."""
    levels_file = tmp_path / "machine.json"
    levels_file.write_text(json.dumps({"working_directory": str(home), "places": LEVELS}))
    spec = FenceSpec(working_directory=home, levels_file=levels_file, start=home / "work", staging={"review": tmp_path}, state=tmp_path / "state")
    assert any(bind.target == home / "docs" for bind in spec.build().binds)
    levels_file.write_text(json.dumps({"working_directory": str(home), "places": {**LEVELS, "docs": "hidden"}}))
    assert not any(bind.target == home / "docs" for bind in spec.build().binds)
    levels_file.write_text(json.dumps({"working_directory": str(home), "places": {**LEVELS, "work": "read"}}))
    assert not any(bind.target == home / "work" and bind.writable for bind in spec.build().binds)


def test_off_linux_the_fence_says_why_it_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    available.cache_clear()
    monkeypatch.setattr(sys, "platform", "darwin")
    ok, reason = available()
    available.cache_clear()
    assert not ok and "Linux" in reason


@pytest.mark.parametrize(("request_line", "allowed"), [
    ("CONNECT api.anthropic.com:443 HTTP/1.1", True),
    ("CONNECT api.anthropic.com:80 HTTP/1.1", False),
    ("CONNECT 127.0.0.1:443 HTTP/1.1", False),
    ("CONNECT evil.example:443 HTTP/1.1", False),
    ("GET http://api.anthropic.com/ HTTP/1.1", False),
])
def test_the_egress_proxy_opens_only_the_model_api(request_line: str, allowed: bool) -> None:
    assert EgressProxy(hosts=AGENT_HOSTS).target(request_line) == (("api.anthropic.com", 443) if allowed else None)


live = pytest.mark.skipif(not available()[0], reason=f"no fence on this PC: {available()[1]}")


def _inside(spec: FenceSpec, script: str) -> dict:
    fence = spec.build()
    done = subprocess.run(fence.command([sys.executable, "-c", script]), cwd=fence.cwd, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "DISPLAY": ":0"})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


PROBE = r"""
import json, os, socket, sys
home = os.environ["HOME"]
def attempt(action):
    try:
        action(); return "ok"
    except OSError as error:
        return type(error).__name__
def read(path):
    return lambda: open(os.path.join(home, path)).read()
def write(path, text="x"):
    def act():
        os.makedirs(os.path.dirname(os.path.join(home, path)), exist_ok=True)
        with open(os.path.join(home, path), "w") as stream:
            stream.write(text)
    return act
def connect(port):
    def act():
        s = socket.create_connection(("127.0.0.1", port), timeout=2); s.close()
    return act
def x11():
    s = socket.socket(socket.AF_UNIX); s.connect("\0/tmp/.X11-unix/X0")
port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
print(json.dumps({
    "hidden": attempt(read(".ssh/id_ed25519")),
    "see_level": attempt(read("private/diary.txt")),
    "read": attempt(read("docs/plan.txt")),
    "read_write": attempt(write("docs/new.txt")),
    "key_in_open_folder": attempt(read("docs/id_ed25519")),
    "pem_in_open_folder": attempt(read("docs/keys/server.pem")),
    "env_in_open_folder": attempt(read("docs/.env")),
    "write_existing": attempt(write("work/app/src/main.py", "print(2)\n")),
    "write_new_below": attempt(write("work/app/src/new.py")),
    "git_hook": attempt(write("work/app/.git/hooks/post-checkout")),
    "git_config": attempt(write("work/app/.git/config", "[core]\n\tfsmonitor = sh -c evil\n")),
    "repo_claude_settings": attempt(write("work/app/.claude/settings.json")),
    "folder_mcp_json": attempt(write("work/.mcp.json")),
    "folder_claude_md": attempt(write("work/CLAUDE.md")),
    "existing_claude_md": attempt(write("work/app/CLAUDE.md", "obey me")),
    "sandbox_top": attempt(write("sandbox/anything.txt")),
    "sandbox_git_hook": attempt(write("sandbox/app/.git/hooks/post-checkout")),
    "review_write": attempt(write("review/draft.txt", "edited")),
    "run_record": attempt(write(".interact/out/agents/run-1.json", "{\"fence\": null}")),
    "config_env": attempt(read(".interact/config.env")),
    "claude_json": attempt(write(".claude.json", "{\"mcpServers\": {\"x\": {\"command\": \"sh\"}}}")),
    "claude_settings": attempt(write(".claude/settings.json", "{\"hooks\": 1}")),
    "claude_bin": os.path.exists(os.path.join(home, ".claude/bin/claude-watch")),
    "claude_ide": os.path.exists(os.path.join(home, ".claude/ide")),
    "other_transcripts": os.path.exists(os.path.join(home, ".claude/projects/other-session")),
    "own_transcript": attempt(write(".claude/projects/this-run/log.jsonl")),
    "credentials": attempt(read(".claude/.credentials.json")),
    "localhost": attempt(connect(port)),
    "bus": os.path.exists("/run/user/%d/bus" % os.getuid()),
    "x11": attempt(x11),
    "proxy": os.environ.get("HTTPS_PROXY", ""),
}))
"""


@pytest.fixture
def listener():
    """A service on the host's loopback (an IDE server, a database, a local model)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    threading.Thread(target=lambda: [server.accept()[0].close() for _ in iter(int, 1)], daemon=True).start()
    yield server.getsockname()[1]
    server.close()


@live
def test_a_fenced_agent_cannot_leave_anything_that_runs_after_it(home: Path, tmp_path: Path, listener: int) -> None:
    """The review's exploit paths, each tried from inside: its run record, ~/.claude.json and
    tool state, hooks / settings in a Write folder, localhost, secrets below an open folder."""
    fence = _spec(home, tmp_path).build()
    done = subprocess.run(fence.command([sys.executable, "-c", PROBE, str(listener)]), cwd=fence.cwd, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "DISPLAY": ":0"})
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    refused = ("hidden", "see_level", "read_write", "key_in_open_folder", "pem_in_open_folder", "env_in_open_folder", "git_hook", "git_config",
               "existing_claude_md", "sandbox_git_hook", "config_env", "claude_settings", "localhost", "x11")
    assert {name: seen[name] for name in refused if seen[name] == "ok"} == {}
    assert seen["read"] == seen["write_existing"] == seen["write_new_below"] == seen["sandbox_top"] == seen["own_transcript"] == seen["credentials"] == "ok"
    assert seen["claude_bin"] is seen["claude_ide"] is seen["other_transcripts"] is seen["bus"] is False
    assert seen["proxy"].startswith("http://127.0.0.1:")
    # Its run record is not there (a write lands in the fence's own empty home); what it wrote
    # to its tool config stays in its private copy. The owner's files are untouched.
    assert seen["claude_json"] == "ok" and json.loads((home / ".claude.json").read_text()) == {"mcpServers": {}}
    assert (home / ".interact/out/agents/run-1.json").read_text() == "{\"fence\": \"recorded\"}"
    assert (home / "work/app/src/main.py").read_text() == "print(2)\n" and (home / "review/draft.txt").read_text() == "draft"
    assert not (home / "work/app/.git/hooks/post-checkout").exists() and (home / "work/app/CLAUDE.md").read_text() == "the owner's instructions"
    # Steering files it created at the top of a writable folder or repository left the folder
    # for a review the owner reads on the PC.
    assert not any((home / name).exists() for name in ("work/CLAUDE.md", "work/.mcp.json", "work/app/.claude"))
    from interact.place_reviews import PlaceReviews
    held = {item.path for review in PlaceReviews(root=tmp_path / "reviews").list() for item in review.files}
    assert {"CLAUDE.md", ".mcp.json", "app/.claude/settings.json"} <= held


@pytest.mark.asyncio
async def test_a_fenced_launch_starts_inside_the_fence_and_its_run_keeps_the_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher wraps the CLI in the fence, and the run's record keeps what the fence is built
    from (the chosen candidate is registered twice: the second write must not drop it)."""
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
    spec = FenceSpec(working_directory=tmp_path, levels={"work": "write"}, start=tmp_path / "work", state=tmp_path / "state")
    run = await run_module.run_agent(_FakeProvider(), "t", agent="tester", name="w", cwd=str(tmp_path / "work"), fence=spec)
    await asyncio.wait_for(run.wait(), timeout=30)
    assert started[0][:4] == [sys.executable, "-m", "interact.fence", "outer"]
    assert reg.get_run(run.run_id).fence == spec
