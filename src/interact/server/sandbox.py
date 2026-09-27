"""The agent-owned isolated display (nested Xephyr/Xvfb) and the cross-platform real-desktop
backend, plus the idle reaper that closes surfaces the agent abandoned. All the mutable
``_sandbox`` / ``_portable`` singletons and their lifecycle live here, apart from the tools that
drive them."""

import asyncio
import atexit
import logging
import signal
from contextlib import suppress
from dataclasses import dataclass

from interact.desktop import DesktopWindow
from interact.server.core import _sessions, config

_log = logging.getLogger("interact")

_sandbox: "object | None" = None  # the headless NestedBackend, created on first launch_app
_sandbox_generation = 0  # bumped every time `_sandbox` is torn down and replaced (#159)
_sandbox_replace_reason: str | None = None  # why the CURRENT _sandbox replaced the previous one


def _get_sandbox(size: str | None = None):
    """The server-owned isolated display (Xephyr if a display is present, else headless Xvfb).
    Created on first use so a window the user moved/buried — or a GPU app that won't screen-grab on
    the real desktop — can be driven in a clean, occlusion-proof, non-intrusive sandbox.

    ``size`` ("WxH") picks the display resolution: a phone app needs a phone-shaped screen, not the
    1280x800 default. The sandbox is a singleton, so a launch that asks for a *different explicit*
    size than the running one transparently respawns it at that size (the first app's size no longer
    wins forever). ``size=None`` means "attach to whatever is already running" — every capture/attach
    tool (screenshot, run_actions, get_interactive_elements, …) passes None, and must NEVER resize a
    live sandbox: collapsing None to the default once respawned a phone (412x915) sandbox at the
    1280x800 default on the first screenshot, so the launched window opened portrait, closed, and
    reopened landscape (empty). None only picks the default when creating a sandbox from cold.

    A long session can exhaust or kill the nested X server (e.g. dozens of leaked GPU apps); the
    cached backend would then reject every launch until restarted. So a dead sandbox is torn down
    and respawned transparently here — the agent never has to manually reset it (#10)."""
    global _sandbox
    if _sandbox is not None:
        if not _sandbox.is_alive():
            # Capture WHY before tearing it down — is_alive() already polled the exit code / X
            # socket, so this is the last chance to read it; a caller mid-attach otherwise just
            # sees an unexplained fresh sandbox with none of its state (#141).
            health_fn = getattr(_sandbox, "display_health", None)
            reason = health_fn() if health_fn is not None else "the nested display stopped answering"
            _close_sandbox(reason)  # dead/hung X server → respawn (size-independent self-heal, #10)
        elif size is not None and _sandbox.size != size:
            _close_sandbox(  # an EXPLICIT new size (launch_app) → respawn at it
                f"another caller's launch_app respawned it at size {size} (was {_sandbox.size})"
            )
    if _sandbox is None:
        from interact.desktop import NestedBackend

        _sandbox = NestedBackend(
            config.nested_display, size or config.nested_size, headless=config.nested_headless
        )
    _sandbox.touch()  # every attach/launch resets idleness — the reaper only closes ABANDONED ones
    return _sandbox


def _close_sandbox(reason: str | None = None) -> None:
    """Tear the sandbox down. ``reason`` is recorded for whoever holds a `SandboxReservation` (or
    reads `last_replace_reason()`) on the instance being replaced — a caller-triggered close
    (`reset_sandbox`, shutdown) passes none, since there nobody is surprised by it (#141/#159)."""
    global _sandbox, _sandbox_generation, _sandbox_replace_reason
    if _sandbox is not None:
        try:
            _sandbox.close()
        finally:
            _sandbox = None
            _sandbox_generation += 1
            _sandbox_replace_reason = reason


def last_replace_reason() -> str | None:
    """Why the sandbox now running is NOT the one a caller last saw, or ``None`` if it was never
    replaced out from under anyone. Read by `targets._sandbox_death_diagnostics` (#141)."""
    return _sandbox_replace_reason


@dataclass
class SandboxReservation:
    """A caller's claim on one sandbox instance, captured by `reserve_sandbox` at attach time — so
    a later caller can tell whether ITS sandbox is still the live singleton, and if not, WHY:
    another caller's `launch_app`/resize (or the idle reaper) replaced it from under it (#159)."""

    backend: object
    generation: int

    def replaced_reason(self) -> str | None:
        """``None`` while `backend` is still the live singleton; else the reason it was replaced."""
        if _sandbox is self.backend and _sandbox_generation == self.generation:
            return None
        return _sandbox_replace_reason or "another caller replaced the sandbox"


def reserve_sandbox(size: str | None = None) -> SandboxReservation:
    """Like `_get_sandbox`, but returns a reservation a caller can later check with
    `SandboxReservation.replaced_reason()` instead of silently getting handed whatever sandbox
    happens to be running now (#159)."""
    backend = _get_sandbox(size)
    return SandboxReservation(backend=backend, generation=_sandbox_generation)


def _close_sandbox_on_signal(signum, _frame) -> None:
    """Exit normally on a termination signal so `atexit` runs and the sandbox is torn down.

    `atexit` does NOT fire on SIGTERM, and SIGTERM is exactly how this project restarts its own
    servers (`server_registry.kill_stale_servers`, `interact doctor --fix`) — so without this the
    most common shutdown path leaked the X server, which is what the user sees as Xephyr windows
    piling up on their desktop.
    """
    raise SystemExit(128 + signum)


def install_teardown_handlers() -> None:
    """Make the sandbox get torn down however this SERVER stops.

    Called from the server lifespan, not at import: a process-wide signal handler is not something
    a `interact agents list` should inherit just for importing this module.
    """
    atexit.register(_close_sandbox)  # the lifespan's `finally` covers a clean stop; this the rest
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if sig is not None:
            with suppress(OSError, ValueError):  # not the main thread, or no such signal here
                signal.signal(sig, _close_sandbox_on_signal)


_portable: "object | None" = None  # the macOS/Windows real-desktop backend (mss + pynput)


def _get_portable():
    """The cross-platform real-desktop backend used for ``target="screen"`` on macOS/Windows,
    created on first use (verified on real mac/win CI runners, #24)."""
    global _portable
    if _portable is None:
        from interact.desktop.backend import PortableBackend

        _portable = PortableBackend()
    return _portable


def _resolve_portable_screen() -> DesktopWindow:
    """A whole-screen DesktopWindow bound to the portable backend — capture (mss) + input (pynput)
    route through it, so ``target="screen"`` drives the real macOS/Windows desktop (#24)."""
    from interact.desktop import _SCREEN_WID

    pb = _get_portable()
    win = DesktopWindow(name="screen", wid=_SCREEN_WID, x=0, y=0, w=pb.screen_w, h=pb.screen_h)
    win._backend = pb
    return win


def _reap_sandbox(ttl: int = 0) -> None:
    """Drop a nested sandbox whose X server has died (``is_alive`` polls it, reaping the zombie) —
    and, with ``ttl`` > 0, one the agent has ABANDONED: agents open the visible Xephyr, finish
    their task, and leave the window on the user's desktop to close by hand. Idle-past-ttl closes
    it exactly like an idle browser session (#36's sibling); the next launch_app respawns fresh.
    A live recording session blocks reaping — the agent is mid-capture."""
    if _sandbox is None:
        return
    if not _sandbox.is_alive():
        health_fn = getattr(_sandbox, "display_health", None)
        reason = health_fn() if health_fn is not None else "the nested display stopped answering"
        _close_sandbox(reason)
        return
    if ttl > 0 and _sandbox.idle_seconds() > ttl and not _sandbox.is_recording_any():
        _log.info("auto-closing sandbox idle for %.0fs", _sandbox.idle_seconds())
        _close_sandbox(f"the idle reaper closed it after {_sandbox.idle_seconds():.0f}s unused")


async def _idle_session_reaper(ttl: int) -> None:
    """Periodically auto-close agent-owned surfaces the agent abandoned: browser sessions idle
    beyond ``ttl`` (an idle Chromium can spin CPU on a left-open page for hours) and a sandbox idle
    beyond ``config.sandbox_idle_ttl`` (its Xephyr is a VISIBLE window the user otherwise has to
    close by hand). Each ttl <= 0 disables its own half; the loop runs while either is enabled."""
    ttls = [t for t in (ttl, config.sandbox_idle_ttl) if t > 0]
    if not ttls:
        return
    interval = min(60, *ttls)
    while True:
        await asyncio.sleep(interval)
        try:
            if ttl > 0:
                closed = await _sessions.close_idle(ttl)
                if closed:
                    _log.info("auto-closed idle browser session(s): %s", ", ".join(closed))
            _reap_sandbox(config.sandbox_idle_ttl)
        except Exception:  # a transient error must never kill the reaper
            _log.exception("idle session reaper error")
