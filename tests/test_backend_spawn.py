"""Backend spawn contract.

Every backend's spawn shares one signature — spawn(argv, cwd=None, env=None), declared once on
DesktopBackend — so launch_app can pass a project directory and a per-launch environment
(`FOO=bar app`, #117) regardless of which backend the sandbox resolved to. `env` layers OVER the
backend's own child environment: the host's for a real session (here), the sandbox's pins for
NestedBackend (test_launch_replace.py).

The child reports what it actually saw: its cwd, the caller's variable, and whether the host's
PATH is still there underneath it.
"""

import sys
from pathlib import Path

import pytest

from interact.desktop.backend import LocalBackend, PortableBackend


# PATH is still there underneath it.
_SPAWN_REPORT = (
    "import os, sys\n"
    "open(sys.argv[1], 'w').write('\\n'.join([os.getcwd(), os.environ['FOO'], str('PATH' in os.environ)]))\n"
)


@pytest.mark.parametrize("backend_cls", [LocalBackend, PortableBackend])
def test_spawn_takes_a_cwd_and_layers_env_over_the_hosts(backend_cls, tmp_path):
    be = backend_cls.__new__(backend_cls)  # no uinput/X/mss needed to exercise spawn
    report = tmp_path / "report.txt"
    proc = be.spawn([sys.executable, "-c", _SPAWN_REPORT, str(report)], cwd=str(tmp_path), env={"FOO": "bar"})
    assert proc.wait(timeout=10) == 0
    cwd, foo, path_kept = report.read_text().splitlines()
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert foo == "bar" and path_kept == "True"
