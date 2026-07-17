"""Every backend's spawn shares one signature — `spawn(argv, cwd=None)` — so launch_app can pass
a project directory regardless of which backend the sandbox resolved to."""

import sys

from interact.desktop.backend import LocalBackend


def test_local_spawn_accepts_a_cwd(tmp_path):
    lb = LocalBackend.__new__(LocalBackend)  # no uinput/X needed to exercise spawn
    proc = lb.spawn([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=str(tmp_path))
    assert proc.wait(timeout=10) == 0
