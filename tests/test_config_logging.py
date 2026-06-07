"""Configurable fallbacks + tool-input debug dumps + no coord leak."""

import json

from interact.config import Config


def test_configurable_fallbacks(monkeypatch):
    monkeypatch.setenv("INTERACT_COMPONENT_FALLBACKS", "gemini/x, zai/y")
    config = Config()
    assert config.fallbacks_for("component") == ["gemini/x", "zai/y"]
    assert config.fallbacks_for("video") == []  # unset → bundled recommendations used


def test_dump_input_writes_both_files(tmp_path):
    from interact.debug_utils import Debug

    inv = str(tmp_path / "inv")
    Debug.dump_input(inv, {"tool": "screenshot", "query": "q"}, {"image_model": "m", "headless": True})
    written = json.loads((tmp_path / "inv" / "tool_input.json").read_text())
    resolved = json.loads((tmp_path / "inv" / "tool_input_resolved.json").read_text())
    assert written["query"] == "q" and written["tool"] == "screenshot"
    assert resolved["image_model"] == "m"  # full effective config with defaults


def test_el_report_never_leaks_coordinates(monkeypatch):
    import interact.dispatch as dispatch

    monkeypatch.setattr(dispatch, "_fmt_cursor", lambda: "default")

    class _El:
        index, role, name, center_x, center_y = 2, "button", "Submit", 137, 451

    report = dispatch._el_report("clicked", _El())
    assert "[2]" in report and "Submit" in report  # ref + name shown
    assert "137" not in report and "451" not in report  # pixel coords NOT leaked


def test_xy_report_is_factual_and_never_leaks_pixels(monkeypatch):
    """A coordinate action is reported factually — no prescriptive 'use refs instead' nudge (it
    fights coordinate-capable agents), but still never echoes the raw pixels back."""
    import interact.dispatch as dispatch

    monkeypatch.setattr(dispatch, "_fmt_cursor", lambda: "default")
    report = dispatch._xy_report("clicked", 137, 451)
    assert "137" not in report and "451" not in report  # no raw pixels echoed
    assert "hint" not in report.lower()  # no prescriptive nudge


def test_raw_xy_snaps_to_detected_element_ref():
    # When a detection exists for the window, a raw x,y inside an element's box resolves to that
    # element (stable ref) instead of staying a blind pixel hit. Smallest box wins (button > panel).
    import interact.dispatch as dispatch
    from interact.desktop import DesktopElement, _element_cache

    wid = 4242
    panel = DesktopElement(index=1, role="panel", name="board", x=0, y=0, w=800, h=800)
    square = DesktopElement(index=2, role="button", name="e4", x=100, y=100, w=100, h=100)
    _element_cache[wid] = [panel, square]
    try:
        assert dispatch._element_at(wid, 150, 150).index == 2  # inside both → smallest (square)
        assert dispatch._element_at(wid, 10, 10).index == 1    # only the panel
        assert dispatch._element_at(wid, 900, 900) is None      # outside everything → no snap
    finally:
        _element_cache.pop(wid, None)


def test_dump_output_records_exact_return_including_errors(tmp_path):
    from interact.debug_utils import Debug

    # plain-string return → written verbatim
    inv = str(tmp_path / "ok")
    Debug.dump_output(inv, "clicked [3] button: 'Play'")
    assert (tmp_path / "ok" / "output.txt").read_text() == "clicked [3] button: 'Play'"

    # an ERROR / no-window-match string is captured too (so a failed call + its retry are auditable)
    inv_err = str(tmp_path / "err")
    Debug.dump_output(inv_err, "No window matching 'Foo'. Available:\n  Bar")
    assert "No window matching" in (tmp_path / "err" / "output.txt").read_text()

    # [text, Image] return (return_image=True) → only the text lands in output.txt
    inv_img = str(tmp_path / "img")
    Debug.dump_output(inv_img, ["window summary text", object()])  # 2nd item stands in for Image
    assert (tmp_path / "img" / "output.txt").read_text() == "window summary text"

    # no invocation dir → no-op, never raises
    Debug.dump_output(None, "ignored")

