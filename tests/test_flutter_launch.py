"""launch_app adds --enable-software-rendering for a Flutter bundle (#28): its GPU blur composites
to a black strip under the sandbox's software GL, so Flutter's Skia CPU rasteriser is used instead.
Pure filesystem detection — no display."""

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
