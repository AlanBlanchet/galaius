"""Lazily provision the isolated venv vision model nodes run inference in.

A bare `interact machine connect` must not carry torch/transformers just because vision nodes
exist on the machine's catalog — most enrolled machines never run one. `ensure_vision_env` builds
`<cache_root>/env` with `uv` on the first vision-node command and reuses it after; the GPU-vs-CPU
torch build is decided once (via `nvidia-smi`) and pinned in a marker file so a later call never
races a second provision or silently re-decides the device mid-fleet-life.

`VisionWorker` keeps one `vision_infer.py` process running in that venv between steps, so a step on
the model the last one used starts inferring at once instead of re-importing torch and re-loading
weights (~6 s on an RTX 2070); it exits after `keep_warm` seconds idle, freeing the GPU.
"""

import collections
import json
import logging
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

#: Reports a phase of the step (`interact_core.MACHINE_PHASES`) with its plain detail.
Report = Callable[[str, str], None]

_TORCH_INDEX = {True: "https://download.pytorch.org/whl/cu124", False: "https://download.pytorch.org/whl/cpu"}


def ensure_vision_env(cache_root: Path, timeout: float = 1800, report: Report = lambda phase, detail: None) -> Path:
    """Return the vision venv's python, creating and provisioning it first if needed."""
    env_dir = cache_root / "env"
    marker = env_dir / ".vision-marker"
    if marker.is_file():
        return env_dir / "bin" / "python"
    report("installing", "installing the vision runtime (torch, transformers) on first use; this can take several minutes")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to provision the vision inference environment (~/.interact/models/env)")
    subprocess.run([uv, "venv", "--python", "3.12", str(env_dir)], check=True, capture_output=True, text=True, timeout=timeout)
    python = env_dir / "bin" / "python"
    gpu = subprocess.run(["nvidia-smi"], capture_output=True, timeout=30).returncode == 0
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), "torch", "torchvision", "--index-url", _TORCH_INDEX[gpu]],
        check=True, capture_output=True, text=True, timeout=timeout,
    )
    subprocess.run(
        # timm backs DETR's ResNet-50 backbone in transformers >= 5.
        [uv, "pip", "install", "--python", str(python), "transformers>=4.50", "timm", "pillow", "numpy"],
        check=True, capture_output=True, text=True, timeout=timeout,
    )
    marker.write_text(json.dumps({"device": "cuda" if gpu else "cpu", "torch_index": _TORCH_INDEX[gpu]}), encoding="utf-8")
    return python


class InferenceFailed(RuntimeError):
    """The worker answered this request with an error: the step fails, the worker (and the model
    it holds) stays."""


class VisionWorker:
    """The long-lived `vision_infer.py` process (its module docstring holds the line protocol).
    One request at a time; a worker that died or hung is replaced on the next request; its phase
    lines reach `report` as they come."""

    def __init__(self, keep_warm: float) -> None:
        self.keep_warm = keep_warm
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=200)
        self._idle: threading.Timer | None = None
        #: Bumped by every request: an idle timer from before one never stops the worker after it.
        self._generation = 0

    def run(self, python: Path, request_path: Path, report: Report, timeout: float = 300) -> dict:
        with self._lock:
            if self._idle is not None:
                self._idle.cancel()
            self._generation += 1
            process = self._started(python)
            try:
                process.stdin.write(json.dumps({"request": str(request_path)}) + "\n")
                process.stdin.flush()
                return self._answer(report, timeout)
            except InferenceFailed:
                raise
            except BaseException:
                # Dead, hung or unreadable: the next request starts a fresh worker.
                self._stop(kill=True)
                raise
            finally:
                if self.keep_warm > 0 and self._process is not None:
                    self._idle = threading.Timer(self.keep_warm, self._expire, args=(self._generation,))
                    self._idle.daemon = True
                    self._idle.start()
                elif self._process is not None:
                    self._stop()

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _expire(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._stop()

    def _started(self, python: Path) -> subprocess.Popen:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._lines, self._stderr = queue.Queue(), collections.deque(maxlen=200)
        script = Path(__file__).with_name("vision_infer.py")
        self._process = subprocess.Popen([str(python), str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        for stream, sink in ((self._process.stdout, self._lines.put), (self._process.stderr, self._stderr.append)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()
        return self._process

    @staticmethod
    def _pump(stream, sink: Callable) -> None:
        for line in stream:
            sink(line)
        sink(None)

    def _answer(self, report: Report, timeout: float) -> dict:
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.start()
        try:
            while not deadline.is_set():
                try:
                    line = self._lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    raise RuntimeError(("".join(line for line in self._stderr if line) or "vision inference stopped").strip()[-2000:])
                message = json.loads(line)
                if "phase" in message:
                    report(str(message["phase"]), str(message.get("detail") or ""))
                elif "error" in message:
                    # The step says what failed (the traceback's last line); the log keeps the rest.
                    trace = str(message["error"]).strip()
                    logger.error("vision inference failed:\n%s", trace)
                    raise InferenceFailed(trace.splitlines()[-1][:400] if trace else "vision inference failed")
                else:
                    return message["result"]
            raise TimeoutError(f"vision inference did not answer in {timeout:g} s")
        finally:
            timer.cancel()

    def _stop(self, kill: bool = False) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.stdin.close()
        try:
            process.wait(timeout=0 if kill else 10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
