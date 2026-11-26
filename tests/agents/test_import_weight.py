"""The processes that sit at rest for hours (queue dispatcher, spawn waiter) import what they use."""

import os
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_agent_dispatcher_loads_no_screen_library():
    """2026-11-24: three dispatchers sat at 390-435 MB; numpy, PIL and playwright (22 MB) came in with a vision type through
    the model registry, none of them used to deliver a message."""
    code = ("import sys, galaius.agents.agent_queue, galaius.agents.run, galaius.agents.spawn_line;"
            "print(sorted(name for name in ('numpy', 'PIL', 'playwright') if name in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": str(ROOT)}).stdout  # Windows starts no interpreter without SYSTEMROOT and its home variables
    assert out.strip() == "[]"
