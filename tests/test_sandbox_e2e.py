"""Opt-in end-to-end reproductions of the sandbox fixes, run against REAL apps in a nested Xephyr —
the way they were verified on the maintainer's machine. Gated on INTERACT_LOCAL_E2E=1 (they spawn
Xephyr + real apps, ~seconds each) and self-skip when an app/display isn't present, so normal CI is
untouched. Run locally with:

    INTERACT_LOCAL_E2E=1 uv run --with PySide6 pytest tests/test_sandbox_e2e.py -v

(#25, sandbox Chrome --app keyboard delivery, is covered by unit tests over the focus path —
test_nested_repaint.py::test_focus_wid_* and test_capture_freshness.py::test_backend_keyboard_* —
plus a manual chrome+CDP reproduction; a live chrome-in-Xephyr test is too flaky to commit.)
"""

import io
import os
import shutil
import sys
import time

import pytest

pytestmark = pytest.mark.desktop  # needs a live Linux display (skipped without one)


def _require_e2e():
    if not os.environ.get("INTERACT_LOCAL_E2E"):
        pytest.skip("opt-in: set INTERACT_LOCAL_E2E=1 (spawns Xephyr + real apps)")
    if shutil.which("Xephyr") is None:
        pytest.skip("Xephyr not installed")


def _biggest(nb):
    best, area = None, -1
    for wid, title in nb.list_windows():
        g = nb.window_geometry(title)
        if g and g[2] * g[3] > area:
            best, area = (wid, title, g), g[2] * g[3]
    return best


def test_qt_combo_popup_is_captured(tmp_path):
    """#31: an override-redirect QComboBox popup is composited into the nested window capture."""
    _require_e2e()
    pytest.importorskip("PySide6")
    from PIL import Image

    from interact.desktop import DesktopWindow
    from interact.desktop_backend import NestedBackend

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


def test_flutter_bundle_navbar_not_black():
    """#28: a Flutter bundle launched via the launch_app path renders its bottom bar (not black)
    thanks to the auto --enable-software-rendering. Set INTERACT_FLUTTER_BUNDLE to a bundle path."""
    _require_e2e()
    bundle = os.environ.get("INTERACT_FLUTTER_BUNDLE")
    if not bundle or not os.path.exists(bundle):
        pytest.skip("set INTERACT_FLUTTER_BUNDLE=<path to a Flutter linux bundle binary>")
    import numpy as np
    from PIL import Image

    from interact.desktop import DesktopWindow
    from interact.desktop_backend import NestedBackend
    from interact.server import _flutter_software_render

    argv, note = _flutter_software_render([bundle])
    assert note, "bundle not detected as Flutter"
    nb = NestedBackend(92, "412x915", headless=False)
    try:
        nb.spawn(argv)
        time.sleep(4.0)
        title = next((t for _, t in nb.list_windows() if t and "aino" in t.lower() and t.count(".") == 0), None)
        title = title or (nb.list_windows()[0][1] if nb.list_windows() else None)
        assert title, "no window appeared"
        g = nb.window_geometry(title)
        win = DesktopWindow(name=title, wid=int(nb._window_id(title)), x=g[0], y=g[1], w=g[2], h=g[3])
        win._backend = nb
        arr = np.asarray(Image.open(io.BytesIO(win.capture())).convert("RGB"))
        frac_black = float((arr[int(arr.shape[0] * 0.88):].mean(axis=2) < 8).mean())
        assert frac_black < 0.5, f"bottom bar is black ({frac_black:.2f})"
    finally:
        nb.close()
