"""The nested sandbox backend — what it captures and how it delivers input.

Repaint: a Flutter/GL app under software GL presents a stale (black) buffer to X until a
configure event makes it repaint — so a fresh launch, or its blurred BottomNavigationBar, captures
solid black (issues #7/#8). The fix: detect the unrendered frame and force a repaint with a 2px
resize nudge, then recapture. WM-less keyboard input also needs an explicit focus (windowfocus, not
windowactivate which needs _NET_ACTIVE_WINDOW — the error that drove a consumer to give up, #6).

Display-free: maim/xdotool are stubbed; the real GL behaviour is verified live in one opt-in
e2e test (test_qt_combo_popup_is_captured_e2e, gated on INTERACT_LOCAL_E2E=1), not elsewhere here.
"""

import io
import subprocess

import pytest
from PIL import Image

from interact.desktop import NestedBackend, _gl_unrendered
from tests.support import solid_png
from tests.support.desktop import bare_nested_backend


@pytest.mark.parametrize(
    "png, expected, why",
    [
        (solid_png((412, 915), (0, 0, 0)), True, "whole frame black → GL surface never painted"),
        (solid_png((412, 915), (230, 230, 230), bottom=(0, 0, 0)), True, "rendered body, black bottom bar (#7)"),
        (solid_png((412, 915), (230, 230, 230), bottom=(0, 0, 0), bottom_frac=0.16), True,
         "taller (~16%) black ConvexAppBar strip is detected too (#14-#20, variable bar height)"),
        (solid_png((412, 915), (230, 230, 230), bottom=(0, 0, 0), bottom_frac=0.08), True, "thin (~8%) black bar detected"),
        (solid_png((412, 915), (230, 230, 230)), False, "fully rendered light UI → leave it alone"),
        (solid_png((412, 915), (8, 8, 10)), False, "genuinely dark theme (dark body too) → don't nudge every grab"),
        (solid_png((412, 915), (240, 240, 240), bottom=(20, 22, 30)), False, "dark-but-painted bar → not black"),
    ],
)
def test_gl_unrendered_heuristic(png, expected, why):
    assert _gl_unrendered(png) is expected, why


def test_double_click_is_one_xdotool_process_with_an_inter_click_delay(monkeypatch):
    """#116, by the #88 rule above NestedBackend.scroll: button events fired as separate processes
    arrive unevenly spaced, so a sequence that must COALESCE (a double-click) is ONE `xdotool click
    --repeat` with an explicit delay — never two mousedown/mouseup process pairs."""
    nb = bare_nested_backend(size=(412, 915))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(nb, "_xdotool", lambda *args: calls.append(args))
    nb.click(5, 6, "left", count=2)
    assert calls[-1] == ("click", "--repeat", "2", "--delay", "60", "1")
    assert ("mousedown", "1") not in calls


def test_capture_window_recovers_from_stale_wid(monkeypatch):
    """`maim -i <wid>` can fail when the wid went stale between enumeration and capture — a
    multi-process app (Chrome) recreates its top-level window (the recurring real-world
    `maim -i N returned non-zero` error). Re-resolve the title once and retry, not crash."""
    nb = bare_nested_backend(size=(412, 915))
    resolved = {"wid": "0x1"}
    monkeypatch.setattr(nb, "_window_id", lambda name: resolved["wid"])
    good = solid_png((412, 915), (230, 230, 230))

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
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")  # no fresh window to find

    def maim(wid):
        raise subprocess.CalledProcessError(1, ["maim", "-i", wid])

    monkeypatch.setattr(nb, "_maim_window", maim)
    screen = solid_png((412, 915), (210, 210, 210))
    monkeypatch.setattr(nb, "capture", lambda: screen)
    monkeypatch.setattr(nb, "force_repaint", lambda name: pytest.fail("fallback frame → no repaint"))
    assert nb.capture_window("chrome") == screen


def test_capture_window_repaints_on_black(monkeypatch):
    """First maim returns black → force a repaint, recapture, return the rendered frame."""
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    frames = iter([solid_png((412, 915), (0, 0, 0)), solid_png((412, 915), (230, 230, 230), bottom=(40, 40, 40))])
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
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    monkeypatch.setattr(nb, "_maim_window", lambda wid: solid_png((412, 915), (0, 0, 0)))  # always black
    repaints = []
    monkeypatch.setattr(nb, "force_repaint", lambda name: repaints.append(name) or True)

    for _ in range(5):
        nb.capture_window("oled")
    assert repaints == ["oled", "oled"], f"a persistently-black UI must be nudged at most twice, got {repaints}"


def test_window_rerendered_after_nudge_is_rearmed(monkeypatch):
    """A window that renders after a nudge clears its attempt counter, so a LATER navigation that
    goes black is nudged again — the heuristic must not permanently give up on a once-good window."""
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    # black → (nudge) rendered ; later black again → (nudge) rendered
    frames = iter([solid_png((412, 915), (0, 0, 0)), solid_png((412, 915), (220, 220, 220)), solid_png((412, 915), (0, 0, 0)), solid_png((412, 915), (220, 220, 220))])
    monkeypatch.setattr(nb, "_maim_window", lambda wid: next(frames))
    repaints = []
    monkeypatch.setattr(nb, "force_repaint", lambda name: repaints.append(name) or True)

    nb.capture_window("aino")  # black→nudge→rendered (re-armed)
    nb.capture_window("aino")  # black→nudge→rendered again
    assert repaints == ["aino", "aino"], "a re-blackened window must be nudged again, not abandoned"
    assert "aino" not in nb._repaint_useless


def test_capture_window_does_not_repaint_when_rendered(monkeypatch):
    """A good first grab returns immediately — no needless nudge (which would reset app scroll)."""
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    calls = {"maim": 0}

    def maim(wid):
        calls["maim"] += 1
        return solid_png((412, 915), (230, 230, 230), bottom=(40, 40, 40))

    monkeypatch.setattr(nb, "_maim_window", maim)
    monkeypatch.setattr(nb, "force_repaint", lambda name: pytest.fail("should not repaint a rendered frame"))

    nb.capture_window("aino")
    assert calls["maim"] == 1


def test_force_repaint_shrinks_then_restores(monkeypatch):
    """The nudge resizes the window smaller by _repaint_delta (60px, capped at h/4) — big enough to
    rebind a blurred bar's Skia layer, not just relayout the body — then back to its exact size."""
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    monkeypatch.setattr(nb, "window_geometry", lambda name: (0, 0, 412, 915))
    monkeypatch.setattr("interact.desktop.nested.time.sleep", lambda *_: None)
    sizes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "interact.desktop.nested.subprocess.run",
        lambda cmd, **k: sizes.append((cmd[3], cmd[4])) if cmd[1] == "windowsize" else None,
    )

    assert nb.force_repaint("aino") is True
    assert sizes == [("412", "855"), ("412", "915")], "shrink by 60px, then restore exactly"


def test_force_repaint_noop_without_window(monkeypatch):
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: None)
    monkeypatch.setattr(nb, "window_geometry", lambda name: None)
    assert nb.force_repaint("ghost") is False


@pytest.mark.parametrize(
    "a,b,expect",
    [
        ((0, 0, 100, 100), (50, 50, 100, 100), True),
        ((0, 0, 600, 400), (11, 35, 578, 210), True),  # the real QComboBox popup case (#31)
        ((0, 0, 100, 100), (100, 0, 100, 100), False),  # edge-touching, not overlapping
        ((0, 0, 50, 50), (900, 900, 50, 50), False),  # far-away popup
    ],
)
def test_rects_overlap(a, b, expect):
    from interact.desktop.backend import _rects_overlap

    assert _rects_overlap(a, b) is expect


def test_composited_grab_expands_to_overlapping_popup(monkeypatch):
    """A mapped override-redirect popup overlapping the window → capture the union region (anchored
    at the window origin, expanded down/right to the popup), so the popup pixels are included (#31)."""
    nb = bare_nested_backend(size=(412, 915))  # screen 412x915
    monkeypatch.setattr(nb, "window_geometry", lambda name: (0, 0, 400, 300))
    monkeypatch.setattr(nb, "_overlay_rects", lambda: [(10, 250, 380, 200)])  # extends to y=450
    region: dict = {}
    monkeypatch.setattr(nb, "_maim_region", lambda x, y, w, h: region.update(r=(x, y, w, h)) or b"R")
    monkeypatch.setattr(nb, "_maim_window", lambda wid: b"W")
    assert nb._composited_grab("app", "0x1") == b"R"
    assert region["r"] == (0, 0, 400, 450)  # window origin, expanded to the popup's bottom


def test_composited_grab_plain_region_grabs_the_window_not_stale_pixmap(monkeypatch):
    """No overlapping popup → still a region grab of the window's OWN rectangle (the live front
    buffer), never `maim -i <wid>` (the backing pixmap that goes stale after a software-GL in-app
    navigation, lagging screenshot a frame behind the element scan, #40/#41). A distant, non-
    overlapping popup is ignored — the region stays the window's own bounds."""
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "window_geometry", lambda name: (0, 0, 200, 200))
    monkeypatch.setattr(nb, "_maim_window", lambda wid: pytest.fail("must not grab the stale by-wid pixmap"))
    region: dict = {}
    monkeypatch.setattr(nb, "_maim_region", lambda x, y, w, h: region.update(r=(x, y, w, h)) or b"R")
    monkeypatch.setattr(nb, "_overlay_rects", lambda: [])
    assert nb._composited_grab("app", "0x1") == b"R"
    assert region["r"] == (0, 0, 200, 200)
    monkeypatch.setattr(nb, "_overlay_rects", lambda: [(900, 900, 50, 50)])  # distant, no overlap
    assert nb._composited_grab("app", "0x1") == b"R"
    assert region["r"] == (0, 0, 200, 200)  # unchanged — the distant popup is ignored


# --- Live counterpart of the composited-grab tests above (from test_sandbox_e2e.py) ---------
# Opt-in: spawns a real Xephyr + a real Qt app. Gated on INTERACT_LOCAL_E2E=1, self-skips when
# Xephyr/PySide6 isn't present, so normal CI is untouched. Run locally with:
#     INTERACT_LOCAL_E2E=1 uv run --with PySide6 pytest tests/test_nested_repaint.py -v -k e2e


def _require_e2e():
    import os
    import shutil

    if not os.environ.get("INTERACT_LOCAL_E2E"):
        pytest.skip("opt-in: set INTERACT_LOCAL_E2E=1 (spawns Xephyr + real apps)")
    if shutil.which("Xephyr") is None:
        pytest.skip("Xephyr not installed")


def test_qt_combo_popup_is_captured_e2e(tmp_path):
    """#31: an override-redirect QComboBox popup is composited into the nested window capture —
    the live version of test_composited_grab_expands_to_overlapping_popup above."""
    _require_e2e()
    pytest.importorskip("PySide6")
    import sys
    import time

    from PIL import Image

    from interact.desktop import DesktopWindow

    app = tmp_path / "qtcombo.py"
    app.write_text(
        "import sys\n"
        "from PySide6.QtWidgets import QApplication, QWidget, QComboBox, QVBoxLayout, QLabel\n"
        "a=QApplication(sys.argv); w=QWidget(); w.setWindowTitle('QtComboE2E'); w.resize(600,400)\n"
        "l=QVBoxLayout(w); l.addWidget(QLabel('pick:'))\n"
        "c=QComboBox(); c.addItems([f'Option-{i}' for i in range(8)]); l.addWidget(c); l.addStretch(1)\n"
        "w.show(); sys.exit(a.exec())\n"
    )

    def band_colors(png):
        im = Image.open(io.BytesIO(png)).convert("RGB").crop((10, 90, 560, 240))
        return len(set(im.getdata()))

    nb = NestedBackend(94, "1000x700", headless=False)
    try:
        nb.spawn([sys.executable, str(app)])
        geo = None
        for _ in range(100):
            if nb._window_id("QtComboE2E"):
                g = nb.window_geometry("QtComboE2E")
                if g and g[2] > 100:
                    geo = g
                    break
            time.sleep(0.3)
        assert geo, "Qt window never appeared"
        x, y, w, h = geo
        win = DesktopWindow(name="QtComboE2E", wid=int(nb._window_id("QtComboE2E")), x=x, y=y, w=w, h=h)
        win._backend = nb
        closed = band_colors(win.capture())
        nb.click(x + 300, y + 52, "left")  # open the combo
        time.sleep(1.0)
        opened = band_colors(win.capture())
        assert opened > closed + 20, f"popup not composited (closed={closed} open={opened})"
    finally:
        nb.close()


def test_nested_captures_hide_the_cursor(monkeypatch):
    """maim superimposes the X pointer by default; every nested grab passes --hidecursor so the
    cursor (parked mid-display) doesn't land in the capture — region grabs read the live root
    framebuffer where the sprite would otherwise show (the visual-critic caught it in a screenshot)."""
    nb = bare_nested_backend(size=(412, 915))
    cmds: list[list[str]] = []

    class _R:
        stdout = b"PNG"

    monkeypatch.setattr(
        "interact.desktop.nested.subprocess.run", lambda cmd, **k: cmds.append(cmd) or _R()
    )
    nb.capture()
    nb._maim_window("0x1")
    nb._maim_region(0, 0, 412, 915)
    assert len(cmds) == 3
    assert all("--hidecursor" in cmd for cmd in cmds), cmds


def test_focus_uses_windowfocus_sync_not_activate(monkeypatch):
    """WM-less, keyboard focus must use windowfocus (XSetInputFocus) — windowactivate needs
    _NET_ACTIVE_WINDOW, which a bare X server rejects (the consumer's xdotool error, #6) — and
    --sync so it settles before the XTEST keystrokes that follow (#25)."""
    nb = bare_nested_backend(size=(412, 915))
    monkeypatch.setattr(nb, "_window_id", lambda name: "0x1")
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        "interact.desktop.nested.subprocess.run",
        lambda cmd, **k: cmds.append(cmd),
    )
    nb.focus("aino")
    # Only xdotool calls are this test's business: patching `subprocess.run` also catches a
    # dependency's platform probe (`uname -p`, from `platform.uname().processor`) that happens
    # to run first, and an exact-list assertion turned that probe into a false failure.
    xdo = [c for c in cmds if c and c[0] == "xdotool"]
    assert xdo == [["xdotool", "windowfocus", "--sync", "0x1"]]
    assert not any("windowactivate" in c for c in cmds)


def test_focus_wid_targets_exact_window_and_skips_empty(monkeypatch):
    """focus_wid focuses a specific wid (so keyboard targets the SAME window click did, #25), and
    no-ops for an empty wid so it never shells out to focus 'nothing'."""
    nb = bare_nested_backend(size=(412, 915))
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        "interact.desktop.nested.subprocess.run", lambda cmd, **k: cmds.append(cmd)
    )
    nb.focus_wid("0x7")
    xdo = [c for c in cmds if c and c[0] == "xdotool"]
    assert xdo == [["xdotool", "windowfocus", "--sync", "0x7"]]
    cmds.clear()
    for empty in (None, 0, "0"):
        nb.focus_wid(empty)
    assert [c for c in cmds if c and c[0] == "xdotool"] == []

