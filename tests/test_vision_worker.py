"""The vision runtime kept warm between steps (`VisionWorker`), driven through its real line
protocol by a stand-in interpreter: phases reach the reporter, a warm worker serves the next step
without restarting, and a worker that errs, dies or hangs fails that step only."""

import json
import sys
from pathlib import Path

import pytest

from interact.vision_env import VisionWorker

#: Answers like `vision_infer.serve`: the request file says what to do. Each start appends to `starts`.
STUB = r'''
import json, sys, time
from pathlib import Path
Path(sys.argv[-2]).open("a").write("start\n")
loaded = False
for line in sys.stdin:
    request = json.loads(Path(json.loads(line)["request"]).read_text())
    if not loaded:
        print(json.dumps({"phase": "loading_model", "detail": "loading m"}), flush=True); loaded = True
    print(json.dumps({"phase": "running_model", "detail": "running m"}), flush=True)
    if request["do"] == "error": print(json.dumps({"error": "Traceback: bad image"}), flush=True)
    elif request["do"] == "die": sys.stderr.write("CUDA out of memory\n"); sys.stderr.flush(); sys.exit(3)
    elif request["do"] == "hang": time.sleep(30)
    else: print(json.dumps({"result": {"count": 2}}), flush=True)
'''


@pytest.fixture
def machine(tmp_path: Path):
    """(a stand-in `python` for the worker, a request writer, the start counter)."""
    (tmp_path / "stub.py").write_text(STUB)
    python = tmp_path / "python"
    python.write_text(f"#!/bin/sh\nexec {sys.executable} {tmp_path / 'stub.py'} {tmp_path / 'starts'} \"$@\"\n")
    python.chmod(0o755)

    def request(do: str) -> Path:
        path = tmp_path / f"{do}.json"
        path.write_text(json.dumps({"do": do}))
        return path
    return python, request, lambda: (tmp_path / "starts").read_text().count("start") if (tmp_path / "starts").exists() else 0


@pytest.mark.parametrize(("keep_warm", "starts", "second_phases"), [(60, 1, ["running_model"]), (0, 2, ["loading_model", "running_model"])])
def test_a_warm_worker_serves_the_next_step_without_reloading(machine, keep_warm, starts, second_phases):
    python, request, started = machine
    worker = VisionWorker(keep_warm)
    phases: list[list[str]] = []
    for _ in range(2):
        seen: list[str] = []
        assert worker.run(python, request("ok"), lambda phase, detail: seen.append(phase)) == {"count": 2}
        phases.append(seen)
    worker.close()
    assert phases == [["loading_model", "running_model"], second_phases]
    assert started() == starts


@pytest.mark.parametrize(("do", "failure", "message", "restarted"), [
    ("error", RuntimeError, "bad image", False),
    ("die", RuntimeError, "CUDA out of memory", True),
    ("hang", TimeoutError, "did not answer in 1 s", True),
])
def test_a_failing_request_fails_its_step_and_the_next_one_still_runs(machine, do, failure, message, restarted):
    python, request, started = machine
    worker = VisionWorker(60)
    with pytest.raises(failure, match=message):
        worker.run(python, request(do), lambda phase, detail: None, timeout=1)
    assert worker.run(python, request("ok"), lambda phase, detail: None) == {"count": 2}
    worker.close()
    assert started() == (2 if restarted else 1)
