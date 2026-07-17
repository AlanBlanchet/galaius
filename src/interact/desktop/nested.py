"""The isolated nested X display (Xephyr visible / Xvfb headless) the agent owns end to end —
the sandbox backend, split out from the base/local/portable backends it extends."""

import os
import shutil
import subprocess
import tempfile
import time

from interact.desktop.backend import (
    DesktopBackend,
    _gl_unrendered,
    _rects_overlap,
    _tail_file,
    _x11_screen_size,
    nested_server_command,
)
from interact.desktop.input import _BUTTONS
from interact.desktop.video import _VideoSession, _ffmpeg_grab_args


class NestedBackend(DesktopBackend):
    """An isolated nested X display the agent owns end to end.

    Starts its own X server — **Xephyr** (visible: rendered as one window on the real
    desktop, so you can watch the agent) or **Xvfb** when ``headless`` (background, no
    window: for CI / servers) — then scopes every action to it with ``DISPLAY=:N``.
    Input goes to the *nested* pointer (not the user's), capture grabs only the nested
    screen. Use :meth:`spawn` to launch the app under test inside it. This is the
    "VM-like" target: reproducible, non-intrusive, and the basis of the desktop test
    suite. Needs ``xdotool`` + ``maim`` plus the chosen server (``apt install
    xserver-xephyr`` / ``xvfb``)."""

    # The sandbox's private audio sink (#47) — class-level defaults so a partially-constructed
    # backend (tests build via __new__) still degrades to video-only instead of AttributeError.
    _audio_sink: str | None = None
    _audio_module: str | None = None
    _last_used: float = 0.0  # monotonic timestamp of the last attach/launch (idle reaping)

    def __init__(self, display: int = 99, size: str = "1280x800", *,
                 headless: bool = False, ready_timeout: float = 5.0):
        self.size = size
        self.headless = headless
        width, height = size.split("x")
        self.screen_w, self.screen_h = int(width), int(height)
        self._procs: list[subprocess.Popen] = []
        self._logs: dict[int, str] = {}  # pid -> temp logfile of a launched app's stdout/stderr
        # Windows whose black frame a repaint did NOT change — an intentionally pure-black/OLED UI,
        # not an unrendered GL buffer. Don't nudge them again (a resize on every capture would reset
        # the app's scroll); see capture_window.
        self._repaint_useless: set[str] = set()
        self._repaint_attempts: dict[str, int] = {}  # per-window nudge count before giving up
        self._repaint_delta = 60  # px the repaint nudge resizes by — big enough to rebind a blur layer
        self._video_sessions: dict[str, _VideoSession] = {}  # name -> live record session (#61/#62)
        self.server_name = "Xvfb" if headless else "Xephyr"
        if shutil.which(self.server_name) is None:
            pkg = "xvfb" if headless else "xserver-xephyr"
            raise RuntimeError(f"{self.server_name} not installed (apt install {pkg})")
        # Start the X server on a FREE display, trying the next free one if a concurrent interact
        # server grabbed it between our check and the server's claim. Hardcoding :99 made several
        # MCP servers fight over it — the loser's Xephyr died seconds in, taking the launched app's
        # windows with it (#33). Picking a free number also sidesteps a stale lock from a crashed
        # prior server.
        last_err: Exception | None = None
        for candidate in self._free_displays(display):
            self.display = f":{candidate}"
            # Force software GL for everything in the sandbox. A nested Xephyr/Xvfb display has no
            # usable hardware GL, so a GPU app (Flutter/Electron/games) that tries hardware EGL hits
            # `DRI2: failed to create any config` and renders BLACK; Mesa's swrast always provides a
            # config. setdefault so an explicit global override still wins.
            self.env = {**os.environ, "DISPLAY": self.display}
            self.env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
            try:
                self._start_server(ready_timeout)
                return
            except RuntimeError as exc:
                last_err = exc  # display raced/unusable → reap the failed server, try the next
        raise last_err or RuntimeError(f"could not start {self.server_name} on any free display")

    @staticmethod
    def _free_displays(preferred: int) -> list[int]:
        """Display numbers to try, free ones first from ``preferred`` up — a display is taken if its
        X lock (``/tmp/.X<n>-lock``) or socket exists. Read-only probe; never writes (#33)."""
        free = [
            n for n in range(preferred, preferred + 64)
            if not os.path.exists(f"/tmp/.X{n}-lock") and not os.path.exists(f"/tmp/.X11-unix/X{n}")
        ]
        return free or [preferred]

    def _start_server(self, ready_timeout: float) -> None:
        """Spawn the X server on ``self.display`` and wait until it answers. On failure, reap the
        process (so a raced display doesn't leave a Xephyr zombie) and re-raise so __init__ can try
        the next display."""
        command = nested_server_command(self.display, self.size, self.headless)
        # Log the X server's own output so a death mid-session can be explained, not just "rc=1".
        self._xserver_log_path = self._open_log(f"{self.server_name.lower()}{self.display}")
        with open(self._xserver_log_path, "wb") as f:
            self._xserver = subprocess.Popen(command, stdout=f, stderr=subprocess.STDOUT)
        try:
            self._await_ready(ready_timeout)
        except RuntimeError:
            self._reap_server()
            raise

    def _reap_server(self) -> None:
        """Tear down (and reap) the X server process + its log — for a failed/raced startup so no
        ``<defunct>`` Xephyr lingers."""
        srv = getattr(self, "_xserver", None)
        if srv is not None:
            if srv.poll() is None:
                srv.terminate()
                try:
                    srv.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    srv.kill()
        path = getattr(self, "_xserver_log_path", None)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _open_log(label: str) -> str:
        # Under ~/.interact/out/sessions/<session>/<date> (not /tmp) so every sandbox log is
        # consolidated with the rest of interact's output and separated by the calling session.
        from interact.runtime import config

        d = config.session_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=f"{label}-", suffix=".log", dir=str(d))
        os.close(fd)
        return path

    def _await_ready(self, timeout: float) -> None:
        """Block until the nested server answers, so spawn/capture don't race startup."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._xserver.poll() is not None:
                tail = _tail_file(self._xserver_log_path, 600)
                why = f": {tail}" if tail else ""
                raise RuntimeError(
                    f"{self.server_name} {self.display} exited (rc={self._xserver.returncode}){why}"
                )
            try:
                _x11_screen_size(self.env)
                return
            except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
                time.sleep(0.1)
        raise RuntimeError(f"{self.server_name} {self.display} did not become ready in {timeout}s")

    def is_alive(self) -> bool:
        """True if the nested X server is still running AND answering. A long session can exhaust the
        display (dozens of leaked GPU apps) so the server dies; the cached backend would then reject
        every launch — even ``xterm`` — until it is respawned (#10)."""
        if self._xserver.poll() is not None:
            return False
        try:
            _x11_screen_size(self.env)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            return False

    def display_health(self) -> str:
        """One-line diagnostic for when a launch produced no window: whether the nested X server is
        alive, and its recent output if it died — so launch_app can explain a dead display instead
        of only listing the generic Qt-helper windows (#33)."""
        if self.is_alive():
            return ""
        tail = _tail_file(getattr(self, "_xserver_log_path", None), 400)
        rc = getattr(getattr(self, "_xserver", None), "returncode", "?")
        return f"The sandbox {self.server_name} {self.display} is DOWN (rc={rc})" + (
            f": {tail}" if tail else " — call reset_sandbox to respawn it."
        )

    def _reap(self) -> None:
        """Drop exited child apps (and unlink their logs) so a long session doesn't accumulate dead
        entries — the leak behind a display that eventually refuses new clients (#10)."""
        alive: list[subprocess.Popen] = []
        for proc in self._procs:
            if proc.poll() is None:
                alive.append(proc)
            else:
                stale = self._logs.pop(proc.pid, None)
                if stale:
                    try:
                        os.unlink(stale)
                    except OSError:
                        pass
        self._procs = alive

    def _ensure_audio_sink(self) -> str | None:
        """The sandbox's private PulseAudio/PipeWire null sink, created on first use. Spawned apps
        get ``PULSE_SINK`` pointing here, so their audio is isolated BOTH ways: it never plays on
        the user's speakers, and a recording of ``<sink>.monitor`` carries the APP's sound only —
        never the user's system audio (#47). Best-effort: no pulse server / no pactl → None, and
        recordings stay video-only."""
        if self._audio_sink is not None:
            return self._audio_sink
        display = getattr(self, "display", None)
        if not display:  # partially-constructed backend (tests) → video-only
            return None
        sink = f"interact_sandbox_{display.lstrip(':')}"
        try:
            done = subprocess.run(
                ["pactl", "load-module", "module-null-sink", f"sink_name={sink}",
                 f"sink_properties=device.description={sink}"],
                capture_output=True, text=True, timeout=5,
            )
            if done.returncode != 0:
                return None
            self._audio_module = done.stdout.strip()
            self._audio_sink = sink
            self.env["PULSE_SINK"] = sink  # every spawn from now on routes audio here
            return sink
        except (OSError, subprocess.SubprocessError):
            return None

    def audio_monitor(self) -> str | None:
        """The pulse source a recording reads the sandbox's audio from, or None (video-only)."""
        sink = self._ensure_audio_sink()
        return f"{sink}.monitor" if sink else None

    def spawn(self, argv: list[str], cwd: str | None = None) -> subprocess.Popen:
        """Launch a process inside the nested display (tracked for teardown), capturing its
        stdout/stderr so a crash can be explained. Reaps previously-exited apps first."""
        self._ensure_audio_sink()  # route the app's audio into the sandbox sink from birth (#47)
        self._reap()
        path = self._open_log("app")
        with open(path, "wb") as f:
            proc = subprocess.Popen(argv, env=self.env, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
        self._procs.append(proc)
        self._logs[proc.pid] = path
        return proc

    def proc_output(self, proc: subprocess.Popen, limit: int = 1500) -> str:
        """Tail of what a launched process wrote (stdout+stderr) — to tell an app crash from a dead
        display in a launch error. Empty if the proc isn't tracked."""
        return _tail_file(self._logs.get(proc.pid), limit)

    def _xdotool(self, *args: str) -> None:
        subprocess.run(["xdotool", *args], env=self.env, check=True)

    def _xdotool_ok(self, *args: str) -> None:
        """Best-effort xdotool that never raises — for repaint/focus nudges where a transient
        failure must not crash a capture."""
        subprocess.run(
            ["xdotool", *args], env=self.env, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def capture(self) -> bytes:
        self._reap()  # drop apps that exited since the last spawn so zombies don't accumulate (#11)
        return subprocess.run(["maim", "--hidecursor"], env=self.env, capture_output=True, check=True).stdout

    def _maim_window(self, wid: str) -> bytes:
        return subprocess.run(["maim", "--hidecursor", "-i", wid], env=self.env, capture_output=True, check=True).stdout

    def _maim_region(self, x: int, y: int, w: int, h: int) -> bytes:
        # --hidecursor: a region grab reads the live root framebuffer, into which maim otherwise
        # superimposes the X pointer sprite — it would land mid-screenshot (parked at screen centre).
        return subprocess.run(
            ["maim", "--hidecursor", "-g", f"{w}x{h}+{x}+{y}"], env=self.env, capture_output=True, check=True
        ).stdout

    def _overlay_rects(self) -> list[tuple[int, int, int, int]]:
        """Absolute ``(x, y, w, h)`` of mapped override-redirect windows on the nested display —
        menus, Qt/GTK combo drop-downs, tooltips. These open as SEPARATE top-level windows (the WM
        is bypassed, so they're root children), which a per-window ``maim -i`` never includes; we
        composite them into the capture (#31). Best-effort: no python-xlib → empty → plain grab."""
        try:
            from Xlib import display as _xdisplay  # lazy: Linux X11 only, optional
        except ImportError:
            return []
        rects: list[tuple[int, int, int, int]] = []
        try:
            disp = _xdisplay.Display(self.env["DISPLAY"])
            try:
                root = disp.screen().root
                for win in root.query_tree().children:
                    attrs = win.get_attributes()
                    if attrs.map_state != 2 or not attrs.override_redirect:  # 2 = IsViewable
                        continue
                    geo = win.get_geometry()
                    if geo.width <= 4 or geo.height <= 4:
                        continue
                    abs_pos = root.translate_coords(win, 0, 0)
                    rects.append((abs_pos.x, abs_pos.y, geo.width, geo.height))
            finally:
                disp.close()
        except Exception:  # any X error → degrade to no overlays, never break a capture
            return []
        return rects

    def _composited_grab(self, name: str, wid: str) -> bytes:
        """``maim`` of the window, expanded right/down to also capture any override-redirect popup
        overlapping it (#31). Anchored at the window's own top-left so image coordinates stay
        window-relative (what the click path expects) — a popup that opens upward/leftward is
        clipped (rare; use target="nested" for that), but a normal downward drop-down is included."""
        try:
            geo = self.window_geometry(name)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError):
            geo = None  # can't read geometry → just grab the window, never fail the capture
        if geo is None:
            return self._maim_window(wid)
        wx, wy, ww, wh = geo
        overlays = [r for r in self._overlay_rects() if _rects_overlap(r, geo)]
        if not overlays:
            # Grab the window's screen REGION (live front buffer), not `maim -i <wid>` (the window's
            # backing pixmap). Under software GL a Flutter surface leaves a STALE pixmap after an
            # in-app navigation — valid pixels of the PREVIOUS screen, so the black-frame nudge never
            # fires — and a by-id grab returns it, lagging screenshot one frame behind the live
            # element scan (#40/#41). The root region always reflects what's actually displayed.
            x0, y0 = max(0, wx), max(0, wy)
            return self._maim_region(x0, y0, min(self.screen_w - x0, ww), min(self.screen_h - y0, wh))
        right = max([wx + ww] + [r[0] + r[2] for r in overlays])
        bottom = max([wy + wh] + [r[1] + r[3] for r in overlays])
        x0, y0 = max(0, wx), max(0, wy)
        x1 = min(self.screen_w, right)
        y1 = min(self.screen_h, bottom)
        return self._maim_region(x0, y0, x1 - x0, y1 - y0)

    def _grab_window(self, name: str, wid: str) -> bytes:
        """Capture the window (compositing in any popup, #31), resilient to a wid that went stale
        between enumeration and capture: a multi-process app (Chrome) recreates its top-level
        window, so the enumerated id can already be dead by the time maim runs (the recurring
        real-world ``maim -i N returned non-zero``). Re-resolve the title once and retry; if the
        window is truly gone, fall back to a full nested-screen grab rather than a hard error."""
        try:
            return self._composited_grab(name, wid)
        except subprocess.CalledProcessError:
            fresh = self._window_id(name)
            if fresh and fresh != wid:
                try:
                    return self._composited_grab(name, fresh)
                except subprocess.CalledProcessError:
                    pass
            return self.capture()  # whole nested display — last resort, never crash-or-black

    def capture_window(self, name: str) -> bytes:
        """PNG of one nested window by title (``maim -i <id>``). Nothing can occlude it here, so this
        is its true content — except a software-GL surface can present a stale black buffer until it
        repaints, so a frame that looks unrendered (:func:`_gl_unrendered`) triggers a repaint nudge
        + recapture. Up to 2 nudges are tried (a blurred ConvexAppBar can need a stronger relayout),
        then the window is left alone so a genuinely-black UI isn't resized on every screenshot. A
        window that renders is re-armed, so a later navigation that goes black is nudged again."""
        self._reap()  # reap exited apps every capture, not only on spawn (#11)
        wid = self._window_id(name)
        if wid is None:
            return self.capture()
        img = self._grab_window(name, wid)
        if _gl_unrendered(img) and name not in self._repaint_useless:
            n = self._repaint_attempts.get(name, 0)
            if n < 2 and self.force_repaint(name):
                self._repaint_attempts[name] = n + 1
                wid = self._window_id(name) or wid
                img = self._grab_window(name, wid)
                if not _gl_unrendered(img):
                    self._repaint_attempts.pop(name, None)  # rendered → re-arm for the next screen
                elif n + 1 >= 2:
                    # Still black after 2 nudges → intentionally black (OLED) or a software-GL
                    # BackdropFilter blur that won't composite under X11 (#14-#20). Stop nudging (and
                    # scroll-resetting) it; the Wayland/sway backend renders this class correctly.
                    self._repaint_useless.add(name)
                    self._repaint_attempts.pop(name, None)
        return img

    def capture_video(self, name: str, duration: float = 3.0, fps: int = 10) -> bytes:
        """Record one nested window via ffmpeg x11grab on THIS display (``DISPLAY=:N``), not ``:0``.
        Recording a sandbox window grabbed the real display and returned all-black frames while
        screenshot() worked (#18). Forces a repaint first so the first frame isn't a black GL
        buffer (same software-GL cause as the still-capture nudge)."""
        geo = self.window_geometry(name)
        x, y, w, h = geo if geo is not None else (0, 0, self.screen_w, self.screen_h)
        self.force_repaint(name)
        fd, out = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            subprocess.run(
                _ffmpeg_grab_args(self.env["DISPLAY"], x, y, w, h, fps, out,
                                  duration=duration, audio_source=self.audio_monitor()),
                env=self.env, check=True, capture_output=True, timeout=duration + 10,
            )
            with open(out, "rb") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

    def start_video(self, name: str, fps: int = 10) -> None:
        """Begin an open-ended recording of one nested window on THIS display (``DISPLAY=:N``). Idempotent
        per window — a second start while one is live is a no-op. Repaints first so the opening frame
        isn't a black GL buffer (same software-GL cause as the still-capture nudge)."""
        if name in self._video_sessions:
            return
        geo = self.window_geometry(name)
        x, y, w, h = geo if geo is not None else (0, 0, self.screen_w, self.screen_h)
        self.force_repaint(name)
        fd, out = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        self._video_sessions[name] = _VideoSession(
            _ffmpeg_grab_args(self.env["DISPLAY"], x, y, w, h, fps, out,
                              duration=None, audio_source=self.audio_monitor()),
            out, env=self.env,
        )

    def stop_video(self, name: str) -> bytes | None:
        session = self._video_sessions.pop(name, None)
        return session.stop() if session else None

    def is_recording(self, name: str) -> bool:
        return name in self._video_sessions

    def is_recording_any(self) -> bool:
        return bool(self._video_sessions)

    def touch(self) -> None:
        """Mark the sandbox as just-used — every attach/launch calls this, so idleness measures
        time since the AGENT last cared, and the idle reaper never closes an actively-driven one."""
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used

    def fit_window(self, name: str) -> bool:
        """Move + size the named window to fill the nested display (origin 0,0, full screen WxH), so
        a mobile/phone app on a phone-shaped display fills it instead of floating as a small
        rectangle in a corner. Best-effort: an app that pins its own size just ignores the resize."""
        wid = self._window_id(name)
        if wid is None:
            return False
        self._xdotool_ok("windowmove", wid, "0", "0")
        self._xdotool_ok("windowsize", wid, str(self.screen_w), str(self.screen_h))
        return True

    def force_repaint(self, name: str) -> bool:
        """Force a full repaint by nudging the window's size (shrink, restore); returns True if it
        nudged. A Flutter/GL app under software GL presents a stale/uninitialised buffer to X until a
        configure event makes it relayout — so a fresh launch (or its blurred bottom bar) captures
        solid black. The resize delta (``_repaint_delta``, default 60px, capped at h/4) is large
        enough to make Skia rebind a blurred bar's layer, not just relayout the body. The repaint
        then persists for later frames. Verified live driving aino's GPU UI in the sandbox."""
        wid = self._window_id(name)
        geo = self.window_geometry(name)
        if wid is None or geo is None:
            return False
        _, _, w, h = geo
        if w < 4 or h < 4:
            return False
        delta = max(2, min(getattr(self, "_repaint_delta", 60), h // 4))
        self._xdotool_ok("windowsize", wid, str(w), str(h - delta))
        time.sleep(0.35)
        self._xdotool_ok("windowsize", wid, str(w), str(h))
        time.sleep(0.4)
        return True

    def focus(self, name: str) -> None:
        """Give the named window X input focus so keyboard events reach it. Resolves by title, then
        delegates to :meth:`focus_wid`."""
        self.focus_wid(self._window_id(name))

    def focus_wid(self, wid) -> None:
        """Give a SPECIFIC window X input focus (XSetInputFocus) so the XTEST keystrokes that
        follow land in it. The sandbox has no window manager, so nothing holds focus by default and
        keys would go nowhere (pointer events route by position regardless). ``--sync`` blocks until
        the server confirms the focus change, so a separate ``xdotool type`` process can't fire
        before focus settles. ``windowfocus`` (not ``windowactivate``, which needs
        ``_NET_ACTIVE_WINDOW`` — the error that drove a consumer to abandon interact, #6) works
        WM-less. Bounded so a window that refuses focus can't hang keyboard input (#25)."""
        if wid in (None, 0, "0"):
            return
        try:
            subprocess.run(
                ["xdotool", "windowfocus", "--sync", str(wid)],
                env=self.env, check=False, timeout=3,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            pass

    def move(self, x: float, y: float) -> None:
        self._xdotool("mousemove", "--sync", str(round(x)), str(round(y)))

    def mouse_down(self, button: str = "left") -> None:
        self._xdotool("mousedown", str(_BUTTONS[button]))

    def mouse_up(self, button: str = "left") -> None:
        self._xdotool("mouseup", str(_BUTTONS[button]))

    def type_text(self, text: str) -> None:
        self._xdotool("type", "--delay", "20", text)

    def scroll(self, clicks: int, horizontal: bool = False) -> None:
        # X11 wheel buttons: 4=up, 5=down, 6=left, 7=right. Horizontal scroll has to use 6/7 — a
        # left/right request used to fall through to a vertical button, so a Flutter horizontal
        # ListView/carousel never moved (#54).
        if horizontal:
            button = "7" if clicks > 0 else "6"
        else:
            button = "4" if clicks > 0 else "5"
        for _ in range(abs(clicks)):
            self._xdotool("click", button)

    def key(self, name: str) -> None:
        self._xdotool("key", name)  # xdotool keysym syntax, e.g. "ctrl+a", "Return"

    def _window_id(self, name: str) -> str | None:
        """The wid of the window titled ``name``. A toolkit spawns several same-/substring-titled
        top-levels: a Flutter app exposes both its app-id window (``com.example.aino``) and the
        titled one (``aino``), and a 10x10 GL/clipboard helper. Title-substring matching hits them
        all, so rank candidates: a RENDERED window beats a black/transient helper (so capture +
        input never land on the unrendered one, #28/#1.4), then the largest. Single match → fast
        path, no grab."""
        visible = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", name],
            env=self.env, capture_output=True, text=True,
        ).stdout.split()
        ids = visible or subprocess.run(
            ["xdotool", "search", "--name", name], env=self.env, capture_output=True, text=True
        ).stdout.split()
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0]

        def _area(wid: str) -> int:
            try:
                info = subprocess.run(
                    ["xdotool", "getwindowgeometry", "--shell", wid],
                    env=self.env, capture_output=True, text=True, check=True,
                ).stdout
                v = dict(ln.split("=", 1) for ln in info.splitlines() if "=" in ln)
                return int(v["WIDTH"]) * int(v["HEIGHT"])
            except (subprocess.SubprocessError, KeyError, ValueError):
                return 0

        def _rank(wid: str) -> tuple[int, int]:
            rendered = 1
            try:
                if _gl_unrendered(self._maim_window(wid)):
                    rendered = 0  # a solid-black helper/transient → deprioritise vs a real window
            except subprocess.CalledProcessError:
                rendered = 0
            return (rendered, _area(wid))

        return max(ids, key=_rank)

    def window_geometry(self, name: str) -> tuple[int, int, int, int] | None:
        """``(x, y, w, h)`` of the first window whose title matches ``name`` (or None)."""
        wid = self._window_id(name)
        if wid is None:
            return None
        info = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", wid],
            env=self.env, capture_output=True, text=True, check=True,
        ).stdout
        vals = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
        return int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"])

    def list_windows(self) -> list[tuple[int, str]]:
        """``(wid, title)`` of named windows on the nested display, one per distinct title. There's
        no WM here, so query X directly. Falls back to non-visible matches: a window that's mapped
        but not yet marked viewable (an app mid-startup) must still be reported, or launch_app's
        poll would say nothing appeared when it did."""
        ids = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", ".+"],
            env=self.env, capture_output=True, text=True,
        ).stdout.split() or subprocess.run(
            ["xdotool", "search", "--name", ".+"], env=self.env, capture_output=True, text=True
        ).stdout.split()
        out: list[tuple[int, str]] = []
        seen: set[str] = set()
        for wid in ids:
            name = subprocess.run(
                ["xdotool", "getwindowname", wid], env=self.env, capture_output=True, text=True
            ).stdout.strip()
            if name and name not in seen:
                seen.add(name)
                out.append((int(wid), name))
        return out

    def close(self) -> None:
        for session in self._video_sessions.values():
            session.stop()  # finalize + reap any live recording before tearing the display down
        self._video_sessions.clear()
        if self._audio_module:  # remove the sandbox's null sink with the sandbox (#47)
            subprocess.run(["pactl", "unload-module", self._audio_module],
                           capture_output=True, timeout=5, check=False)
            self._audio_module = self._audio_sink = None
        for proc in (*self._procs, self._xserver):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for path in (*self._logs.values(), getattr(self, "_xserver_log_path", None)):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._logs.clear()


