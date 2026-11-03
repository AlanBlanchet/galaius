"""ffmpeg screen-grab arg construction and the per-session video recorder — the capture
primitives shared by the real-display window path and the nested backend."""

import os
import subprocess
import tempfile
import time


def _ffmpeg_grab_args(
    display_spec: str, x: int, y: int, w: int, h: int, fps: int, out: str,
    duration: float | None = None, audio_source: str | None = None,
) -> list[str]:
    """ffmpeg x11grab argv for one region of ``display_spec`` (``:N`` or ``:N.0``). ``duration=None``
    records open-ended until stopped (a non-blocking session, #61/#62); a number adds ``-t`` for the
    blocking one-shot clip. ``audio_source`` (a PulseAudio/PipeWire source, e.g. a null sink's
    ``.monitor``) muxes the app's audio into the mp4 (#47) — video-only when None. Shared by the
    still-clip and session paths so both encode identically."""
    args = [
        "ffmpeg", "-y", "-f", "x11grab", "-video_size", f"{w}x{h}",
        "-framerate", str(fps), "-i", f"{display_spec}+{max(0, x)},{max(0, y)}",
    ]
    if audio_source:
        args += ["-f", "pulse", "-i", audio_source]
    args += ["-c:v", "libx264", "-preset", "ultrafast"]
    if audio_source:
        args += ["-c:a", "aac"]
    if duration is not None:
        args += ["-t", str(duration)]
    args += ["-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", out]
    return args


class _VideoSession:
    """A live, non-blocking ffmpeg x11grab recording: spawn now, :meth:`stop` later for the mp4 bytes.

    Stopping sends ``q`` on ffmpeg's stdin so it finalizes the moov atom and writes a valid, seekable
    mp4 — a bare SIGTERM truncates the moov and corrupts the clip. Falls back to terminate/kill if the
    graceful quit hangs, and always reads then unlinks the temp file."""

    def __init__(self, args: list[str], out: str, env: dict | None = None):
        self.out = out
        self.proc = subprocess.Popen(
            args, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def stop(self) -> bytes:
        try:
            self.proc.communicate(input=b"q", timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except (OSError, ValueError):
            pass
        try:
            with open(self.out, "rb") as fh:
                return fh.read()
        except OSError:
            return b""
        finally:
            try:
                os.unlink(self.out)
            except OSError:
                pass


