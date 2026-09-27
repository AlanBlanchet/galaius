"""Flutter bundle detection: launch_app adds --enable-software-rendering for a Flutter bundle.

#28: its GPU blur composites to a black strip under the sandbox's software GL, so Flutter's
Skia CPU rasteriser is used instead. Pure filesystem detection — no display.
"""

import os
import time

import pytest

from interact.server import _flutter_software_render




def _flutter_bundle(root, name="app"):
    (root / "data" / "flutter_assets").mkdir(parents=True)
    exe = root / name
    exe.write_text("")
    return exe


def test_detects_flutter_bundle_and_adds_flag(tmp_path):
    exe = _flutter_bundle(tmp_path / "fb")
    argv, note = _flutter_software_render([str(exe)])
    assert argv[-1] == "--enable-software-rendering"
    assert "software-rendering" in note


def test_detects_through_env_prefix(tmp_path):
    exe = _flutter_bundle(tmp_path / "fb")
    argv, _ = _flutter_software_render(["env", "LIBGL_ALWAYS_SOFTWARE=1", str(exe)])
    assert argv[-1] == "--enable-software-rendering"


def test_idempotent(tmp_path):
    exe = _flutter_bundle(tmp_path / "fb")
    argv, note = _flutter_software_render([str(exe), "--enable-software-rendering"])
    assert argv == [str(exe), "--enable-software-rendering"] and note == ""


def test_detects_via_embedder_lib(tmp_path):
    root = tmp_path / "fb2"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "libflutter_linux_gtk.so").write_text("")
    exe = root / "app"
    exe.write_text("")
    assert _flutter_software_render([str(exe)])[0][-1] == "--enable-software-rendering"


def test_non_flutter_command_untouched(tmp_path):
    plain = tmp_path / "nf"
    plain.mkdir()
    exe = plain / "xterm"
    exe.write_text("")
    assert _flutter_software_render([str(exe)]) == ([str(exe)], "")
    assert _flutter_software_render(["xterm"]) == (["xterm"], "")


# --- Live counterpart of the Flutter detection tests above (from test_sandbox_e2e.py) -------
# Opt-in: spawns a real Xephyr + a real Flutter linux bundle. Gated on INTERACT_LOCAL_E2E=1 and
# INTERACT_FLUTTER_BUNDLE=<path to bundle binary>; self-skips without either. Run locally with:
#     INTERACT_LOCAL_E2E=1 INTERACT_FLUTTER_BUNDLE=<path> uv run --with PySide6 \
#         pytest tests/test_launch_replace.py -v -k e2e


def test_flutter_bundle_navbar_not_black_e2e():
    """#28: a Flutter bundle launched via the launch_app path renders its bottom bar (not black)
    thanks to the auto --enable-software-rendering detected by the unit tests above."""
    import io
    import shutil

    if not os.environ.get("INTERACT_LOCAL_E2E"):
        pytest.skip("opt-in: set INTERACT_LOCAL_E2E=1 (spawns Xephyr + real apps)")
    if shutil.which("Xephyr") is None:
        pytest.skip("Xephyr not installed")
    bundle = os.environ.get("INTERACT_FLUTTER_BUNDLE")
    if not bundle or not os.path.exists(bundle):
        pytest.skip("set INTERACT_FLUTTER_BUNDLE=<path to a Flutter linux bundle binary>")
    import numpy as np
    from PIL import Image

    from interact.desktop import DesktopWindow, NestedBackend

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

