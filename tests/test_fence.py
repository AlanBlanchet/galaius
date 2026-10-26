"""The agent fence (`galaius.fence`): an agent CLI started from this PC's levels sees only the
folders set to read or later, gets a private copy of its tool state, reaches only its model API
through the runner's egress proxy, and cannot leave anything that runs later outside the fence
(its run record, the owner's tool config, git hooks, editor / Claude settings). Every level is
read again at each turn. Live checks run where this PC can build the fence (Linux with
bubblewrap and Landlock scopes); elsewhere the fence says why it is unavailable."""

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from galaius.agents import providers
from galaius.agents.providers import CodexProvider
from galaius_core import MachineFence
from galaius_core.wire import fitted

from galaius import fence
from galaius.fence import EGRESS, Confinement, EgressProxy, FenceSpec, available, confinement_refusal, fenced, userns_refusal

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
        "work/app/src/AGENTS.md": "the owner's nested instructions",
        "sandbox/app/.git/hooks/pre-commit.sample": "#!/bin/sh\n",
        "sandbox/app/.git/config": "[core]\n",
        ".claude/settings.json": "{}",
        ".claude/.credentials.json": "{\"token\": 1}",
        ".claude/bin/claude-watch": "#!/bin/sh\n",
        ".claude/ide/50442.lock": "{\"authToken\": \"secret\"}",
        ".claude/projects/other-session/log.jsonl": "someone else's transcript",
        ".claude.json": "{\"mcpServers\": {}}",
        ".galaius/out/agents/run-1.json": "{\"fence\": \"recorded\"}",
        ".galaius/config.env": "OPENAI_API_KEY=x",
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
                     state=tmp_path / "state", reviews=tmp_path / "reviews", **{"providers": ("claude",), "egress": AGENT_HOSTS, **fields})


def test_binds_follow_the_levels_and_nothing_else_of_home(home: Path, tmp_path: Path) -> None:
    fence = _spec(home, tmp_path).build()
    binds = {(bind.source, bind.target, bind.writable) for bind in fence.binds}
    assert (home / "docs", home / "docs", False) in binds
    assert (tmp_path / "staging-review", home / "review", True) in binds  # writes land in the staging copy, never the folder
    assert not any(bind.target in {home / "private", home / ".ssh", home, home / ".galaius"} for bind in fence.binds)


def test_a_write_on_review_folder_without_its_staging_copy_is_refused(home: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="staging copy"):
        FenceSpec(working_directory=home, levels={"review": "write_on_review"}, start=home / "review", state=tmp_path / "state").build()


def test_the_start_folder_must_have_a_level_the_owner_set(home: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="no level"):
        _spec(home, tmp_path, start="private").build()


@pytest.mark.parametrize("planted", [False, True])
def test_a_project_folder_opens_alone_and_never_through_a_link(home: Path, tmp_path: Path, planted: bool) -> None:
    projects = home / ".galaius-projets"
    (projects / "other").mkdir(parents=True)
    if planted:
        (projects / "aino").symlink_to(home / ".galaius")
    else:
        (projects / "aino").mkdir()
    spec = _spec(home, tmp_path, start=".galaius-projets/aino", levels={}, project=projects / "aino")
    if planted:
        with pytest.raises(PermissionError, match="plain project folder"):
            spec.build()
        return
    binds = {(bind.target, bind.writable) for bind in spec.build().binds}
    assert (projects / "aino", True) in binds and not any(target in {projects, projects / "other", home / ".galaius"} for target, _ in binds)


def test_the_desktop_and_session_sockets_are_unset_inside(home: Path, tmp_path: Path) -> None:
    command = _spec(home, tmp_path).build().command(["true"])
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR"):
        assert command[command.index(name) - 1] == "--unsetenv"
    assert command[:4] == [sys.executable, "-m", "galaius.fence", "outer"] and "--unshare-net" in command


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
    "existing_nested_agents_md": attempt(write("work/app/src/AGENTS.md", "obey me")),
    "nested_claude_md": attempt(write("work/app/src/deep/CLAUDE.md", "obey me")),
    "other_key": os.environ.get("GEMINI_API_KEY", ""),
    "own_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    "sandbox_top": attempt(write("sandbox/anything.txt")),
    "sandbox_git_hook": attempt(write("sandbox/app/.git/hooks/post-checkout")),
    "review_write": attempt(write("review/draft.txt", "edited")),
    "run_record": attempt(write(".galaius/out/agents/run-1.json", "{\"fence\": null}")),
    "config_env": attempt(read(".galaius/config.env")),
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
                          env={**os.environ, "DISPLAY": ":0", "GEMINI_API_KEY": "other-provider-key", "ANTHROPIC_API_KEY": "own-key"})
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    refused = ("hidden", "see_level", "read_write", "key_in_open_folder", "pem_in_open_folder", "env_in_open_folder", "git_hook", "git_config",
               "existing_claude_md", "existing_nested_agents_md", "sandbox_git_hook", "config_env", "claude_settings", "localhost", "x11")
    assert {name: seen[name] for name in refused if seen[name] == "ok"} == {}
    assert seen["read"] == seen["write_existing"] == seen["write_new_below"] == seen["sandbox_top"] == seen["own_transcript"] == seen["credentials"] == "ok"
    assert seen["claude_bin"] is seen["claude_ide"] is seen["other_transcripts"] is seen["bus"] is False
    assert seen["proxy"].startswith("http://127.0.0.1:")
    # Its run record is not there (a write lands in the fence's own empty home); what it wrote
    # to its tool config stays in its private copy. The owner's files are untouched.
    assert seen["claude_json"] == "ok" and json.loads((home / ".claude.json").read_text()) == {"mcpServers": {}}
    assert (home / ".galaius/out/agents/run-1.json").read_text() == "{\"fence\": \"recorded\"}"
    assert (home / "work/app/src/main.py").read_text() == "print(2)\n" and (home / "review/draft.txt").read_text() == "draft"
    assert not (home / "work/app/.git/hooks/post-checkout").exists() and (home / "work/app/CLAUDE.md").read_text() == "the owner's instructions"
    # Steering files it created at the top of a writable folder or repository left the folder
    # for a review the owner reads on the PC.
    assert not any((home / name).exists() for name in ("work/CLAUDE.md", "work/.mcp.json", "work/app/.claude"))
    from galaius.place_reviews import PlaceReviews
    held = {item.path for review in PlaceReviews(root=tmp_path / "reviews").list() for item in review.files}
    assert {"CLAUDE.md", ".mcp.json", "app/.claude/settings.json", "app/src/deep/CLAUDE.md"} <= held
    assert not (home / "work/app/src/deep/CLAUDE.md").exists()
    # Only the keys of the CLI the run uses reach it.
    assert seen["other_key"] == "" and seen["own_key"] == "own-key"


@pytest.mark.asyncio
async def test_a_fenced_launch_starts_inside_the_fence_and_its_run_keeps_the_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher wraps the CLI in the fence, and the run's record keeps what the fence is built
    from (the chosen candidate is registered twice: the second write must not drop it)."""
    import asyncio

    from galaius.agents import registry as reg
    from galaius.agents import run as run_module
    from tests.support.agents import install_provider, use_policy
    from tests.test_agent_run import _FakeProvider

    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    install_provider(monkeypatch, _FakeProvider())
    started: list[list[str]] = []
    monkeypatch.setattr(run_module, "contained", lambda argv, **named: started.append(list(argv)) or [sys.executable, "-c", "pass"])
    (tmp_path / "work").mkdir()
    spec = FenceSpec(working_directory=tmp_path, levels={"work": "write"}, start=tmp_path / "work", state=tmp_path / "state")
    run = await run_module.run_agent(_FakeProvider(), "t", agent="tester", name="w", cwd=str(tmp_path / "work"), fence=spec)
    await asyncio.wait_for(run.wait(), timeout=30)
    assert started[0][:4] == [sys.executable, "-m", "galaius.fence", "outer"]
    assert reg.get_run(run.run_id).fence == spec


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux only")
@pytest.mark.parametrize("stderr,code,landlock,mode,started", [
    ("", 0, None, "workspace-write", "codex"),  # its own sandbox starts: Codex as before
    ("bwrap: setting up uid map: Permission denied", 1, None, "workspace-write", "confined"),  # AppArmor-unconfined service, Ubuntu 24.04
    ("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", 1, None, "read-only", "confined"),
    ("bwrap: setting up uid map: Permission denied", 1, "this kernel's Landlock (ABI 0) cannot confine writes", "workspace-write", "refused"),
    ("bwrap: Can't find source path /nope: No such file or directory", 1, None, "workspace-write", "codex"),  # not a namespace refusal
])
def test_codex_runs_in_galaius_confinement_where_its_own_sandbox_cannot_start(tmp_path, monkeypatch, stderr, code, landlock, mode, started):
    """A refused user namespace starts Codex with no sandbox of its own inside Galaius's confinement (writing
    in the start folder only for workspace-write), its galaius server relayed and pre-approved; refused only
    where Landlock cannot confine either."""
    bwrap = tmp_path / "bwrap"
    bwrap.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit {code}\n")
    bwrap.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(providers, "userns_refusal", userns_refusal)
    monkeypatch.setattr(providers, "confinement_refusal", lambda: landlock)
    provider = CodexProvider()
    monkeypatch.setattr(provider, "available", lambda: True)
    monkeypatch.setattr(provider, "executable", lambda: "/opt/codex/bin/codex")
    mesh = json.dumps({"mcpServers": {"galaius": {"command": "galaius", "args": ["mcp"], "env": {"GALAIUS_PARENT_RUN_ID": "r1"}}}})
    permission = {"workspace-write": "workspace_write", "read-only": "read_only"}[mode]
    argv = provider.command("task", cwd=str(tmp_path), model=None, mcp_config=mesh, run_id="r1", permission_mode=permission)
    resumed = provider.resume_command("thread", "next", permission_mode=permission, mcp_config=mesh)
    assert (provider.sandbox_refusal() is not None) is (started == "refused")
    assert 'mcp_servers.galaius.default_tools_approval_mode="approve"' in argv
    if started != "confined":
        assert argv[0] == "codex" and argv[argv.index("--sandbox") + 1] == mode and 'mcp_servers.galaius.args=["mcp"]' in argv
        assert f'sandbox_mode="{mode}"' in resumed  # never the danger-full-access a confined first turn saved
        return
    for started_argv, inner_mode in ((argv, ["--sandbox", "danger-full-access"]), (resumed, ["-c", 'sandbox_mode="danger-full-access"'])):
        assert started_argv[:4] == [sys.executable, "-m", "galaius.fence", "confine"] and started_argv[5:7] == ["--", "/opt/codex/bin/codex"]
        confinement = Confinement.model_validate_json(started_argv[4])
        assert confinement.write_start is (mode == "workspace-write") and confinement.relayed.argv == ("galaius", "mcp")
        inner = started_argv[6:]
        assert " ".join(inner_mode) in " ".join(inner) and "{relay}" in next(part for part in inner if part.startswith("mcp_servers.galaius.args="))


@pytest.mark.skipif(not sys.platform.startswith("linux") or confinement_refusal() is not None, reason="needs Landlock ABI 3 and a known processor")
def test_a_confined_child_writes_only_in_its_folder_and_makes_no_unix_socket(tmp_path, monkeypatch, capfd):
    """The kernel enforces it: a write beside the start folder and a UNIX socket (the session bus reaches
    `systemd-run --user`, outside every confinement) are refused; the start folder and the network are not."""
    class Narrow(Confinement):
        TEMPORARY = ()  # pytest's folders are temporary ones
    (tmp_path / "work").mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(tmp_path / "agents"))
    probe = """
import json, os, socket
def attempt(action):
    try:
        action()
        return "ok"
    except OSError as error:
        return error.errno
print(json.dumps({"start": attempt(lambda: open("in.txt", "w").write("x")), "outside": attempt(lambda: open("../outside/x", "w")),
                  "unix": attempt(lambda: socket.socket(socket.AF_UNIX)), "inet": attempt(lambda: socket.socket(socket.AF_INET).close())}))
"""
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        code = Narrow(write_start=True, home_variable="FIXTURE_HOME", default_home=".fixture").run([sys.executable, "-I", "-c", probe])
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
    assert code == 0
    assert json.loads(capfd.readouterr().out.strip().splitlines()[-1]) == {"start": "ok", "outside": errno.EACCES, "unix": errno.EACCES, "inet": "ok"}
    assert not (tmp_path / "outside" / "x").exists() and not list((tmp_path / "agents" / "confined").iterdir())


def test_the_longest_fence_reason_keeps_its_root_command_in_the_field_the_web_shows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The AppArmor refusal is the longest thing this PC says about its fence, and the contract
    bounds the field the web shows it in (`MachineFence.reason`). What the cut drops is bubblewrap's
    own line, never the one-time root command — and the view itself never raises (2026-10-15: it
    did, and the Data page showed « 1 validation error for MachineFence … »)."""
    restricted = tmp_path / "apparmor_restrict_unprivileged_userns"
    restricted.write_text("1\n")
    monkeypatch.setattr(fence, "_APPARMOR_USERNS", restricted)
    monkeypatch.setattr(fence, "_BWRAP_PASSED", set())
    monkeypatch.setattr(fence.subprocess, "run", lambda *_args, **_options: subprocess.CompletedProcess(
        [], 1, "", "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\nbwrap: setting up uid map: Permission denied\n"))
    refusal = fence.userns_refusal("bwrap")
    assert refusal is not None
    shown = fitted(MachineFence, "reason", refusal)
    assert "sudo apt install apparmor-profiles" in shown and "sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict" in shown
    assert MachineFence(platform="linux", available=False, on=False, reason=shown).reason == shown


def test_a_runtime_whose_python_links_through_a_folder_link_starts_inside_and_stays_read_only(home: Path, tmp_path: Path) -> None:
    """An installed runtime (a uv venv): `bin/python` -> uv's `cpython-3.x` folder link -> the interpreter. The fence,
    built by that runtime's own process (a PC runner, a resume dispatcher), re-makes the link and binds this runtime
    read-only, never another runtime or the runner's data beside it."""
    python = Path(os.path.realpath(sys.executable))
    uv = home / ".local/share/uv/python"
    uv.mkdir(parents=True)
    (uv / "cpython-3.x-linux").symlink_to(python.parent.parent, target_is_directory=True)
    (uv / "cpython-3-linux").symlink_to("cpython-3.x-linux", target_is_directory=True)  # uv's minor-version link: relative
    runtimes = home / ".local/share/galaius/runtimes"
    runtime, other = runtimes / "0.1-new", runtimes / "0.1-old"
    (runtime / "bin").mkdir(parents=True)
    (other / "bin").mkdir(parents=True)
    (runtimes.parent / "machine.json").write_text("{\"key\": 1}")
    (runtime / "bin/python").symlink_to(uv / "cpython-3-linux/bin" / python.name)
    (runtime / "pyvenv.cfg").write_text(f"home = {uv / 'cpython-3-linux/bin'}\ninclude-system-site-packages = false\n")
    build = f"""
import json, os, subprocess, sys
from pathlib import Path
from galaius.fence import FenceSpec, available
spec = FenceSpec(working_directory=Path({str(home)!r}), levels={{"work": "write"}}, start=Path({str(home / "work")!r}), state=Path({str(tmp_path / "state")!r}))
fence = spec.build()
probe = "import json, os, sys; print(json.dumps([sys.prefix, os.path.exists({str(other)!r}), os.path.exists({str(runtimes.parent / 'machine.json')!r}), os.access({str(runtime)!r}, os.W_OK)]))"
inside = subprocess.run(fence.command([sys.executable, "-c", probe]), capture_output=True, text=True, timeout=60) if available()[0] else None
print(json.dumps({{"links": [[str(a), str(b)] for a, b in fence.links], "binds": [[str(b.target), b.writable] for b in fence.binds],
                  "inside": None if inside is None else [inside.returncode, inside.stdout, inside.stderr[-300:]]}}))
"""
    done = subprocess.run([str(runtime / "bin/python"), "-c", build], capture_output=True, text=True, timeout=90,
                          env={**os.environ, "HOME": str(home), "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)})
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert [str(uv / "cpython-3-linux"), "cpython-3.x-linux"] in seen["links"] and [str(uv / "cpython-3.x-linux"), str(python.parent.parent)] in seen["links"]
    assert [str(runtime), False] in seen["binds"] and not any(target.startswith((str(other), str(runtimes.parent / "machine.json"))) for target, _ in seen["binds"])
    if seen["inside"] is None:
        pytest.skip(f"no fence on this PC to start it in: {available()[1]}")
    code, out, err = seen["inside"]
    assert code == 0, err
    assert json.loads(out) == [str(runtime), False, False, False]


@live
def test_a_routed_codex_keeps_the_token_galaius_hands_it_and_no_other_key_the_runner_adds(home: Path, tmp_path: Path) -> None:
    """A codex run routed to a self-hosted model gets its token as `GALAIUS_OPENAI_COMPAT_KEY` in the child's environment
    only: it reaches the agent; the endpoint's source key and another provider's key added the same way do not."""
    fence = _spec(home, tmp_path, providers=("codex",)).build()
    names = ("GALAIUS_OPENAI_COMPAT_KEY", "VLLM_API_KEY", "GEMINI_API_KEY")
    probe = f"import json, os; print(json.dumps([os.environ.get(name, '') for name in {names!r}]))"
    done = subprocess.run(fence.command([sys.executable, "-c", probe]), cwd=fence.cwd, capture_output=True, text=True, timeout=60,
                          env={**os.environ, **{name: f"{name.lower()}-value" for name in names}})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["galaius_openai_compat_key-value", "", ""]


@pytest.mark.parametrize("cli, kept, dropped", [("claude", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"), ("codex", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")])
def test_a_turn_is_fenced_for_its_own_cli_alone(home: Path, tmp_path: Path, cli: str, kept: str, dropped: str) -> None:
    """A PC run's fence is recorded for every CLI it may fall through to (`MachineAgents._fence`); each turn is fenced
    for the CLI that runs it: its keys, its login and tool state, its program and its model's hosts — never the other
    CLI's (a claude turn kept codex's OPENAI_API_KEY, its ~/.codex and api.openai.com, and the other way round)."""
    for folder in (".claude", ".codex"):
        (home / folder).mkdir(exist_ok=True)
    spec = _spec(home, tmp_path, providers=("claude", "codex"), programs=(Path("/usr/bin/claude"), Path("/usr/bin/codex"), Path("/usr/bin/galaius")),
                 egress=(*EGRESS["claude"], *EGRESS["codex"], "galaius.ai"))
    own = spec.only(cli)
    other = "codex" if cli == "claude" else "claude"
    assert own.providers == (cli,)
    assert [program.name for program in own.programs] == [cli, "galaius"]
    assert set(own.egress) == {*EGRESS[cli], "galaius.ai"}
    assert spec.providers == ("claude", "codex")   # the recorded spec is unchanged: a later turn may run the other CLI
    with pytest.raises(ValueError, match="not a CLI this run's fence was built for"):
        spec.only("gemini")
    if not available()[0]:
        pytest.skip(f"no fence on this PC to start it in: {available()[1]}")
    probe = f"import json, os; print(json.dumps([os.environ.get(name, '') for name in {(kept, dropped)!r}] + [os.path.isdir(os.path.expanduser('~/.{other}'))]))"
    done = subprocess.run(fenced([sys.executable, "-c", probe], spec, cli), cwd=own.build().cwd, capture_output=True, text=True, timeout=60,
                          env={**os.environ, kept: "own-key", dropped: "other-key"})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["own-key", "", False]
