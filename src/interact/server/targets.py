"""Resolve the one ``target`` param to a concrete surface — a browser session, a desktop window
(by title / wid), the whole screen, or the nested sandbox — plus the small target-shaped helpers
(``file:<path>`` image source, editor-window guard, element resolution by ref/selector/index)."""

from pathlib import Path

from interact.desktop.atspi import AtSpi
from interact.browser import BrowserManager
from interact.desktop import DesktopElement, DesktopWindow
from interact.server import core, sandbox
from interact.server.core import _DEFAULT_SESSION, _NO_WINDOWS_MSG, config


def _desktop_unsupported(is_screen: bool = False) -> str | None:
    """``"ERROR: …"`` when the requested desktop target isn't available on this OS; ``None`` when it
    is. On Linux everything works. Off Linux (macOS/Windows) the cross-platform PortableBackend
    drives the whole screen, so a ``screen`` target works — but window-title targets (no window
    enumeration yet) and the nested Xephyr sandbox (Linux-only) don't, so those get one clear
    actionable message steering to ``target="screen"`` or the browser tools (#24)."""
    from interact.desktop.backend import desktop_supported

    if desktop_supported() or is_screen:
        return None
    import platform as _pf

    return (
        f"ERROR: on {_pf.system()} only target=\"screen\" desktop automation is available (the "
        "portable mss/pynput backend drives the whole screen); window-title targets and the nested "
        "sandbox (launch_app) are Linux-only. Browser automation works fully — omit `target`. "
        "Track native per-window macOS/Windows support: "
        "https://github.com/AlanBlanchet/interact/issues/24"
    )


def _resolve_nested_target(spec: str) -> tuple[DesktopWindow | None, None, str | None]:
    """Resolve target="nested" (whole sandbox screen) or "nested:<title>" (one sandbox window)."""
    try:
        backend = sandbox._get_sandbox()
    except RuntimeError as e:  # nested server (Xephyr/Xvfb) not installed
        return None, None, f"ERROR: sandbox unavailable — {e}"
    title = spec.split(":", 1)[1].strip() if ":" in spec else ""
    if not title:  # whole nested screen
        win = DesktopWindow(name="sandbox", wid=0, x=0, y=0, w=backend.screen_w, h=backend.screen_h)
        win._backend = backend
        return win, None, None
    win = DesktopWindow.find_in(backend, title)
    if win is None:
        windows = backend.list_windows()
        if windows:
            avail = "\n".join(f'  target="nested:{n}"' for _, n in windows)
            return None, None, f"No sandbox window titled '{title}'. In the sandbox:\n{avail}"
        # Empty sandbox. The old "(none — launch_app first)" misled an agent that had JUST launched —
        # the real cause is the display being respawned (a size change pre-#50/#53, or exhaustion
        # after many GPU launches) and dropping the app. Steer recovery INSIDE the sandbox and forbid
        # the real-desktop fallback: a real session bailed to DISPLAY=:0 xdotool/import on the user's
        # actual desktop, which is exactly what the isolated sandbox exists to avoid.
        return None, None, (
            f"No sandbox window titled '{title}' — the sandbox has no windows right now. "
            f"If you just called launch_app, the display was respawned and dropped the app: call "
            f"launch_app again (or reset_sandbox for a clean display), then retry. Do NOT drive the "
            f"real desktop (xdotool / import / DISPLAY=:0) — keep everything in the isolated sandbox."
        )
    return win, None, None


# Title suffixes of developer tools whose window titles embed project/file names — a partial match
# on one of these is almost always an accident (the query names the APP, whose window lives in the
# sandbox or elsewhere), and driving it types into the user's editor.
_EDITOR_TITLE_MARKERS = (
    "visual studio code", "vs code", "intellij", "pycharm", "webstorm", "goland", "clion",
    "rider", "android studio", "sublime text", "neovim", "gnome-terminal", "konsole", "alacritty",
)


def _looks_like_editor(name: str) -> bool:
    low = name.lower()
    return any(m in low for m in _EDITOR_TITLE_MARKERS)


def _find_desktop_window(title: str) -> DesktopWindow | str:
    windows = DesktopWindow.all()
    if not windows:
        return _NO_WINDOWS_MSG
    t = title.strip()
    if t.lower().startswith("wid:"):  # exact, unambiguous targeting by window id (#5)
        raw = t[4:].strip()
        try:
            wid = int(raw, 0)  # accepts decimal or 0x-hex (as xwininfo prints)
        except ValueError:
            return f"Invalid window id '{raw}' — use the wid shown by list_desktop_windows."
        match = next((w for w in windows if w.wid == wid), None)
        return match or f"No window with wid {raw}. Available:\n{DesktopWindow.listing(windows)}"
    matches = DesktopWindow.matching(t, windows)
    if not matches:
        return f"No window matching '{title}'. Available:\n{DesktopWindow.listing(windows)}"
    # An exact title (sorted first by matching()) or a sole partial match is unambiguous; several
    # partial matches with no exact one would be a silent guess — make the agent pick (#1.3).
    hint = title.strip().lower()
    if any(w.name.lower() == hint for w in matches):
        return matches[0]
    if len(matches) == 1:
        # A lone PARTIAL match that looks like an editor/terminal window (its title merely CONTAINS
        # the query — "shared.rs - aino - Visual Studio Code") is the user's IDE, not the app: the
        # app is usually running in the sandbox instead. Driving it silently typed into the user's
        # editor (10x in client logs) — require explicit targeting.
        if _looks_like_editor(matches[0].name):
            return (
                f"'{title}' only matches the editor/terminal window "
                f"'{matches[0].name}' (wid:{matches[0].wid}) — refusing to drive it on a partial "
                f"match. If the app runs in the sandbox, use target=\"nested:{title}\"; to really "
                f"drive this window, pass its exact title or target=\"wid:{matches[0].wid}\"."
            )
        return matches[0]
    return (
        f"'{title}' matches {len(matches)} windows — pass a more specific or the exact title:\n"
        f"{DesktopWindow.listing(matches)}"
    )


def _resolve_target(
    target: str | None,
    session: str,
) -> tuple[DesktopWindow | None, BrowserManager | None, str | None]:
    """Resolve the one `target` param to a surface. ``None``/``"browser"`` → the browser session
    named by `session` (the default). Any other string → a desktop window matched by title.
    Unifies the old `window`/`session` split into a single "what am I driving?" choice."""
    config.refresh()  # ~/.interact/config.env is the source of truth: pick up live edits per call
    is_desktop = bool(target) and target.strip().lower() != "browser"
    if is_desktop and session != _DEFAULT_SESSION:
        return None, None, "Cannot combine a desktop `target` with a browser `session`"
    if is_desktop:
        t = target.strip()
        is_screen = t.lower() == "screen" or t.lower().startswith("screen:")
        if unsupported := _desktop_unsupported(is_screen):
            return None, None, unsupported
        from interact.desktop.backend import desktop_supported

        if is_screen and not desktop_supported():
            return sandbox._resolve_portable_screen(), None, None  # macOS/Windows whole-screen
        if t.lower() == "nested" or t.lower().startswith("nested:"):
            return _resolve_nested_target(t)
        if is_screen:
            result = DesktopWindow.screen(t)
        else:
            result = _find_desktop_window(t)
        if isinstance(result, str):
            return None, None, result
        return result, None, None
    return None, core._sessions.get(session), None


def _resolve_image_source(target: str | None) -> tuple[bytes | None, str | None]:
    """A ``target="file:<path>"`` reads an EXISTING image file instead of capturing — so
    screenshot/review_ui/measure_ui can judge an artifact produced out-of-band (a saved capture, a
    script's output) without the capture clobbering it (#44). Returns ``(bytes, None)`` for a file
    target, ``(None, "ERROR: …")`` if it can't be read, or ``(None, None)`` for a normal target."""
    if not (target and target.strip().lower().startswith("file:")):
        return None, None
    p = target.strip()[5:]
    if p.startswith("//"):  # tolerate file:// and file:/// URL forms
        p = p[2:]
    try:
        return Path(p).read_bytes(), None
    except OSError as e:
        return None, f"ERROR: could not read image file {p!r} — {e}"


def _resolve_desktop_el(
    wid: int,
    win_name: str,
    *,
    ref: str | None = None,
    selector: str | None = None,
    element: int | None = None,
) -> DesktopElement | None:
    if ref:
        return DesktopElement.get_by_index(wid, DesktopElement.ref_to_index(ref))
    if selector:
        return AtSpi.find_element_by_name(win_name, selector)
    if element is not None:
        return DesktopElement.get_by_index(wid, element)
    return None


def _name_not_found_msg(win_name: str, name: str) -> str:
    elements = AtSpi.detect_elements(win_name)
    if not elements:
        return f"No element with name='{name}' (no elements detected via AT-SPI)"
    names = sorted({e.name for e in elements if e.name})[:10]
    return f"No element with name='{name}'. Available: {', '.join(repr(n) for n in names)}"
