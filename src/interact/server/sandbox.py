"""The agent-owned isolated display (nested Xephyr/Xvfb) and the cross-platform real-desktop
backend, plus the idle reaper that closes surfaces the agent abandoned. All the mutable
``_sandbox`` / ``_portable`` singletons and their lifecycle live here, apart from the tools that
drive them."""

import asyncio
import logging

from interact.desktop import DesktopWindow
from interact.server.core import _sessions, config

_log = logging.getLogger("interact")

_sandbox: "object | None" = None  # the headless NestedBackend, created on first launch_app


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
            _close_sandbox()  # dead/hung X server → respawn (size-independent self-heal, #10)
        elif size is not None and _sandbox.size != size:
            _close_sandbox()  # an EXPLICIT new size (launch_app) → respawn at it
    if _sandbox is None:
        from interact.desktop.backend import NestedBackend

        _sandbox = NestedBackend(
            config.nested_display, size or config.nested_size, headless=config.nested_headless
        )
    _sandbox.touch()  # every attach/launch resets idleness — the reaper only closes ABANDONED ones
    return _sandbox


def _close_sandbox() -> None:
    global _sandbox
    if _sandbox is not None:
        try:
            _sandbox.close()
        finally:
            _sandbox = None


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
        _close_sandbox()
        return
    if ttl > 0 and _sandbox.idle_seconds() > ttl and not _sandbox.is_recording_any():
        _log.info("auto-closing sandbox idle for %.0fs", _sandbox.idle_seconds())
        _close_sandbox()


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
