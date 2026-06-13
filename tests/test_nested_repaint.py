"""Nested sandbox: a Flutter/GL app under software GL presents a stale (black) buffer to X until a
configure event makes it repaint — so a fresh launch, or its blurred BottomNavigationBar, captures
solid black (issues #7/#8). The fix: detect the unrendered frame and force a repaint with a 2px
resize nudge, then recapture. WM-less keyboard input also needs an explicit focus (windowfocus, not
windowactivate which needs _NET_ACTIVE_WINDOW — the error that drove a consumer to give up, #6).

Display-free: maim/xdotool are stubbed; the real GL behaviour is verified live, not here.
"""

import io

import pytest
from PIL import Image

from interact.desktop_backend import NestedBackend, _gl_unrendered


def _png(fill=(0, 0, 0), size=(412, 915), bottom=None, bottom_frac=0.12) -> bytes:
    """A PNG of a solid colour, optionally with a differently-coloured bottom strip."""
    im = Image.new("RGB", size, fill)
    if bottom is not None:
        w, h = size
        im.paste(bottom, (0, int(h * (1 - bottom_frac)), w, h))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "png, expected, why",
    [
        (_png((0, 0, 0)), True, "whole frame black → GL surface never painted"),
        (_png((230, 230, 230), bottom=(0, 0, 0)), True, "rendered body, black bottom bar (#7)"),
        (_png((230, 230, 230)), False, "fully rendered light UI → leave it alone"),
        (_png((8, 8, 10)), False, "genuinely dark theme (dark body too) → don't nudge every grab"),
        (_png((240, 240, 240), bottom=(20, 22, 30)), False, "dark-but-painted bar → not black"),
    ],
)
def test_gl_unrendered_heuristic(png, expected, why):
    assert _gl_unrendered(png) is expected, why


def _backend_no_server() -> NestedBackend:
    """A NestedBackend instance without starting an X server (we stub every subprocess call)."""
    nb = NestedBackend.__new__(NestedBackend)
    nb.env = {"DISPLAY": ":88"}
    nb.screen_w, nb.screen_h = 412, 915
    nb._repaint_useless = set()
    return nb


def test_capture_window_repaints_on_black(monkeypatch):
    """First maim returns black → force a repaint, recapture, return the rendered frame."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    frames = iter([_png((0, 0, 0)), _png((230, 230, 230), bottom=(40, 40, 40))])
    monkeypatch.setattr(nb, "_maim_window", lambda wid: next(frames))
    repainted = []
    monkeypatch.setattr(nb, "force_repaint", lambda name: repainted.append(name) or True)

    img = nb.capture_window("aino")

    assert repainted == ["aino"], "a black grab must trigger exactly one repaint nudge"
    assert not _gl_unrendered(img), "recaptured frame after repaint is rendered"


def test_intentionally_black_ui_is_nudged_at_most_once(monkeypatch):
    """A genuinely pure-black (#000000 / OLED) UI stays black after a repaint — so it must NOT be
    nudged on every capture (a resize would reset the app's scroll). Nudge once; if the frame is
    still black, remember it and skip the nudge thereafter (#9)."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    monkeypatch.setattr(nb, "_maim_window", lambda wid: _png((0, 0, 0)))  # always black
    repaints = []
    monkeypatch.setattr(nb, "force_repaint", lambda name: repaints.append(name) or True)

    nb.capture_window("oled")  # black → one nudge, still black → marked useless
    nb.capture_window("oled")  # must NOT nudge again
    nb.capture_window("oled")
    assert repaints == ["oled"], f"a persistently-black UI must be nudged at most once, got {repaints}"


def test_capture_window_does_not_repaint_when_rendered(monkeypatch):
    """A good first grab returns immediately — no needless nudge (which would reset app scroll)."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    calls = {"maim": 0}

    def maim(wid):
        calls["maim"] += 1
        return _png((230, 230, 230), bottom=(40, 40, 40))

    monkeypatch.setattr(nb, "_maim_window", maim)
    monkeypatch.setattr(nb, "force_repaint", lambda name: pytest.fail("should not repaint a rendered frame"))

    nb.capture_window("aino")
    assert calls["maim"] == 1


def test_force_repaint_shrinks_then_restores(monkeypatch):
    """The nudge resizes the window 2px smaller, then back to its exact original size."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    monkeypatch.setattr(nb, "window_geometry", lambda name: (0, 0, 412, 915))
    monkeypatch.setattr("interact.desktop_backend.time.sleep", lambda *_: None)
    sizes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "interact.desktop_backend.subprocess.run",
        lambda cmd, **k: sizes.append((cmd[3], cmd[4])) if cmd[1] == "windowsize" else None,
    )

    assert nb.force_repaint("aino") is True
    assert sizes == [("412", "913"), ("412", "915")], "shrink by 2px, then restore exactly"


def test_force_repaint_noop_without_window(monkeypatch):
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: None)
    monkeypatch.setattr(nb, "window_geometry", lambda name: None)
    assert nb.force_repaint("ghost") is False


def test_focus_uses_windowfocus_not_activate(monkeypatch):
    """WM-less, keyboard focus must use windowfocus (XSetInputFocus) — windowactivate needs
    _NET_ACTIVE_WINDOW, which a bare X server rejects (the consumer's xdotool error)."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        "interact.desktop_backend.subprocess.run",
        lambda cmd, **k: cmds.append(cmd),
    )
    nb.focus("aino")
    assert cmds == [["xdotool", "windowfocus", "0x1"]]
    assert not any("windowactivate" in c for c in cmds)
