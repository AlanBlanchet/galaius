"""Nested sandbox: a Flutter/GL app under software GL presents a stale (black) buffer to X until a
configure event makes it repaint — so a fresh launch, or its blurred BottomNavigationBar, captures
solid black (issues #7/#8). The fix: detect the unrendered frame and force a repaint with a 2px
resize nudge, then recapture. WM-less keyboard input also needs an explicit focus (windowfocus, not
windowactivate which needs _NET_ACTIVE_WINDOW — the error that drove a consumer to give up, #6).

Display-free: maim/xdotool are stubbed; the real GL behaviour is verified live, not here.
"""

import io
import subprocess

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
        (_png((230, 230, 230), bottom=(0, 0, 0), bottom_frac=0.16), True,
         "taller (~16%) black ConvexAppBar strip is detected too (#14-#20, variable bar height)"),
        (_png((230, 230, 230), bottom=(0, 0, 0), bottom_frac=0.08), True, "thin (~8%) black bar detected"),
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
    nb._procs, nb._logs = [], {}
    nb._repaint_useless = set()
    nb._repaint_attempts = {}
    return nb


def test_capture_window_recovers_from_stale_wid(monkeypatch):
    """`maim -i <wid>` can fail when the wid went stale between enumeration and capture — a
    multi-process app (Chrome) recreates its top-level window (the recurring real-world
    `maim -i N returned non-zero` error). Re-resolve the title once and retry, not crash."""
    nb = _backend_no_server()
    resolved = {"wid": "0x1"}
    monkeypatch.setattr(nb, "_window_id", lambda name: resolved["wid"])
    good = _png((230, 230, 230))

    def maim(wid):
        if wid == "0x1":  # the stale id the window was enumerated under
            resolved["wid"] = "0x2"  # …it has since been recreated under a fresh id
            raise subprocess.CalledProcessError(1, ["maim", "-i", "0x1"])
        return good

    monkeypatch.setattr(nb, "_maim_window", maim)
    monkeypatch.setattr(nb, "force_repaint", lambda name: pytest.fail("rendered frame → no repaint"))
    assert nb.capture_window("chrome") == good


def test_capture_window_falls_back_to_screen_when_window_gone(monkeypatch):
    """If the window is truly gone (re-resolve gives the same dead id, maim keeps failing), return a
    whole-nested-screen grab so the agent still gets pixels — never a hard error."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")  # no fresh window to find

    def maim(wid):
        raise subprocess.CalledProcessError(1, ["maim", "-i", wid])

    monkeypatch.setattr(nb, "_maim_window", maim)
    screen = _png((210, 210, 210))
    monkeypatch.setattr(nb, "capture", lambda: screen)
    monkeypatch.setattr(nb, "force_repaint", lambda name: pytest.fail("fallback frame → no repaint"))
    assert nb.capture_window("chrome") == screen


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


def test_persistently_black_ui_is_nudged_at_most_twice(monkeypatch):
    """A frame that STAYS black after a repaint is either a genuine OLED UI or a software-GL blur
    that won't composite under X11 (#14-#20). Try at most twice (a stubborn blur can need a second,
    stronger relayout), then stop nudging so a real OLED UI isn't resized (scroll-reset) on every
    capture (#9)."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    monkeypatch.setattr(nb, "_maim_window", lambda wid: _png((0, 0, 0)))  # always black
    repaints = []
    monkeypatch.setattr(nb, "force_repaint", lambda name: repaints.append(name) or True)

    for _ in range(5):
        nb.capture_window("oled")
    assert repaints == ["oled", "oled"], f"a persistently-black UI must be nudged at most twice, got {repaints}"


def test_window_rerendered_after_nudge_is_rearmed(monkeypatch):
    """A window that renders after a nudge clears its attempt counter, so a LATER navigation that
    goes black is nudged again — the heuristic must not permanently give up on a once-good window."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    # black → (nudge) rendered ; later black again → (nudge) rendered
    frames = iter([_png((0, 0, 0)), _png((220, 220, 220)), _png((0, 0, 0)), _png((220, 220, 220))])
    monkeypatch.setattr(nb, "_maim_window", lambda wid: next(frames))
    repaints = []
    monkeypatch.setattr(nb, "force_repaint", lambda name: repaints.append(name) or True)

    nb.capture_window("aino")  # black→nudge→rendered (re-armed)
    nb.capture_window("aino")  # black→nudge→rendered again
    assert repaints == ["aino", "aino"], "a re-blackened window must be nudged again, not abandoned"
    assert "aino" not in nb._repaint_useless


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
    """The nudge resizes the window smaller by _repaint_delta (60px, capped at h/4) — big enough to
    rebind a blurred bar's Skia layer, not just relayout the body — then back to its exact size."""
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
    assert sizes == [("412", "855"), ("412", "915")], "shrink by 60px, then restore exactly"


def test_force_repaint_noop_without_window(monkeypatch):
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: None)
    monkeypatch.setattr(nb, "window_geometry", lambda name: None)
    assert nb.force_repaint("ghost") is False


def test_focus_uses_windowfocus_sync_not_activate(monkeypatch):
    """WM-less, keyboard focus must use windowfocus (XSetInputFocus) — windowactivate needs
    _NET_ACTIVE_WINDOW, which a bare X server rejects (the consumer's xdotool error, #6) — and
    --sync so it settles before the XTEST keystrokes that follow (#25)."""
    nb = _backend_no_server()
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        "interact.desktop_backend.subprocess.run",
        lambda cmd, **k: cmds.append(cmd),
    )
    nb.focus("aino")
    assert cmds == [["xdotool", "windowfocus", "--sync", "0x1"]]
    assert not any("windowactivate" in c for c in cmds)


def test_focus_wid_targets_exact_window_and_skips_empty(monkeypatch):
    """focus_wid focuses a specific wid (so keyboard targets the SAME window click did, #25), and
    no-ops for an empty wid so it never shells out to focus 'nothing'."""
    nb = _backend_no_server()
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        "interact.desktop_backend.subprocess.run", lambda cmd, **k: cmds.append(cmd)
    )
    nb.focus_wid("0x7")
    assert cmds == [["xdotool", "windowfocus", "--sync", "0x7"]]
    cmds.clear()
    for empty in (None, 0, "0"):
        nb.focus_wid(empty)
    assert cmds == []
