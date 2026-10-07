"""Whatever program started it, a galaius command runs the active runtime's code; `galaius
upgrade` never hands off; a supervisor handover that never started is rolled back."""

import json
import os
import subprocess
import sys
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from galaius.upgrade.release import BuildIdentity
from galaius.upgrade.store import Runtime, RuntimeReceipt, RuntimeStore

UV = shutil.which("uv")
pytestmark = pytest.mark.skipif(UV is None, reason="needs uv to make a runtime")
SAYS = 'import sys\nprint("{version} ran", " ".join(sys.argv[1:]))\n'


def fake(store: RuntimeStore, version: str, minutes: int) -> Runtime:
    """A real interpreter (a bare uv venv, on any OS) whose `galaius` only says who ran and with what."""
    path = store.root / f"{version}-fake"
    subprocess.run([UV, "venv", "--quiet", "--python", sys.executable, str(path)], check=True)
    site = next(path.glob("lib/python*/site-packages"), path / "Lib" / "site-packages")
    (site / "galaius").mkdir()
    (site / "galaius" / "__init__.py").write_text("")
    (site / "galaius" / "__main__.py").write_text(SAYS.format(version=version))
    build = BuildIdentity(version=version, released_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=minutes), commit=f"{minutes:07x}")
    (path / "installation.json").write_text(RuntimeReceipt(build=build, identity=f"{minutes:064x}", source="local", installed_at=datetime.now(UTC), packages={}).model_dump_json())
    return Runtime(path=path)


def galaius(store: RuntimeStore, *arguments: str, **environment: str) -> str:
    env = {name: value for name, value in os.environ.items() if name != "GALAIUS_SUPERVISED"}
    env.update({"GALAIUS_SUPERVISE": "1", "GALAIUS_RUNTIMES": str(store.root), **environment})
    return subprocess.run([sys.executable, "-m", "galaius", *arguments], env=env, capture_output=True, text=True, timeout=60).stdout


@pytest.fixture
def store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(root=tmp_path / "runtimes")


def test_a_command_started_from_an_older_install_runs_the_active_runtime(store: RuntimeStore) -> None:
    store.activate(fake(store, "0.9.0", 1))
    assert galaius(store, "agents", "list").strip() == "0.9.0 ran agents list"


@pytest.mark.parametrize(("arguments", "environment"), [(("upgrade", "--help"), {}), (("--version",), {"GALAIUS_SUPERVISED": "{}"})])
def test_upgrade_commands_and_workers_run_their_own_code(store: RuntimeStore, arguments, environment) -> None:
    store.activate(fake(store, "0.9.0", 1))
    assert "0.9.0 ran" not in galaius(store, *arguments, **environment)


def test_a_supervisor_handover_that_never_started_is_rolled_back(store: RuntimeStore) -> None:
    previous, broken = fake(store, "0.9.0", 1), fake(store, "0.9.1", 2)
    store.activate(previous)
    store.activate(broken)
    store.hand_over(broken)
    marker = json.loads(store.handover_path.read_text())
    store.handover_path.write_text(json.dumps({**marker, "at": time.time() - 3600}))
    assert galaius(store, "agents", "list").strip() == "0.9.0 ran agents list"
    assert store.pointer().active == previous.path and broken.receipt().identity in store.pointer().failed


def test_a_windowless_start_with_no_stdout_still_hands_off(store: RuntimeStore) -> None:
    """The Windows logon task runs pythonw: sys.stdout and sys.stderr are None there."""
    store.activate(fake(store, "0.9.0", 1))
    env = {**os.environ, "GALAIUS_SUPERVISE": "1", "GALAIUS_RUNTIMES": str(store.root)}
    env.pop("GALAIUS_SUPERVISED", None)
    script = "import sys; sys.stdout = sys.stderr = None; sys.argv = ['galaius', 'machine', 'service', 'run']; from galaius.cli import main; main()"
    ran = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert ran.stdout.strip() == "0.9.0 ran machine service run", ran.stderr
