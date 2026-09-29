"""Whatever program started it, an interact command runs the active runtime's code; `interact
upgrade` never hands off; a supervisor handover that never started is rolled back."""

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from interact.upgrade.release import BuildIdentity
from interact.upgrade.store import Runtime, RuntimeReceipt, RuntimeStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake runtimes are shebang scripts")

SAYS = textwrap.dedent('''\
    #!{python}
    import sys
    print("{version} ran", " ".join(sys.argv[1:]))
    ''')


def fake(store: RuntimeStore, version: str, minutes: int) -> Runtime:
    path = store.root / f"{version}-fake"
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "python").write_text(SAYS.format(python=sys.executable, version=version))
    (path / "bin" / "python").chmod(0o755)
    build = BuildIdentity(version=version, released_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=minutes), commit=f"{minutes:07x}")
    (path / "installation.json").write_text(RuntimeReceipt(build=build, identity=f"{minutes:064x}", source="local", installed_at=datetime.now(UTC), packages={}).model_dump_json())
    return Runtime(path=path)


def interact(store: RuntimeStore, *arguments: str, **environment: str) -> str:
    env = {name: value for name, value in os.environ.items() if name != "INTERACT_SUPERVISED"}
    env.update({"INTERACT_SUPERVISE": "1", "INTERACT_RUNTIMES": str(store.root), **environment})
    return subprocess.run([sys.executable, "-m", "interact", *arguments], env=env, capture_output=True, text=True, timeout=60).stdout


@pytest.fixture
def store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(root=tmp_path / "runtimes")


def test_a_command_started_from_an_older_install_runs_the_active_runtime(store: RuntimeStore) -> None:
    store.activate(fake(store, "0.9.0", 1))
    assert interact(store, "agents", "list").strip() == "0.9.0 ran -I -m interact agents list"


@pytest.mark.parametrize(("arguments", "environment"), [(("upgrade", "--help"), {}), (("--version",), {"INTERACT_SUPERVISED": "{}"})])
def test_upgrade_commands_and_workers_run_their_own_code(store: RuntimeStore, arguments, environment) -> None:
    store.activate(fake(store, "0.9.0", 1))
    assert "0.9.0 ran" not in interact(store, *arguments, **environment)


def test_a_supervisor_handover_that_never_started_is_rolled_back(store: RuntimeStore) -> None:
    previous, broken = fake(store, "0.9.0", 1), fake(store, "0.9.1", 2)
    store.activate(previous)
    store.activate(broken)
    store.hand_over(broken)
    marker = json.loads(store.handover_path.read_text())
    store.handover_path.write_text(json.dumps({**marker, "at": time.time() - 3600}))
    assert interact(store, "agents", "list").strip() == "0.9.0 ran -I -m interact agents list"
    assert store.pointer().active == previous.path and broken.receipt().identity in store.pointer().failed


def test_a_windowless_start_with_no_stdout_still_hands_off(store: RuntimeStore) -> None:
    """The Windows logon task runs pythonw: sys.stdout and sys.stderr are None there."""
    store.activate(fake(store, "0.9.0", 1))
    env = {**os.environ, "INTERACT_SUPERVISE": "1", "INTERACT_RUNTIMES": str(store.root)}
    env.pop("INTERACT_SUPERVISED", None)
    script = "import sys; sys.stdout = sys.stderr = None; sys.argv = ['interact', 'machine', 'service', 'run']; from interact.cli import main; main()"
    ran = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert ran.stdout.strip() == "0.9.0 ran -I -m interact machine service run", ran.stderr
