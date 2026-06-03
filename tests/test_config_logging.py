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


def test_xy_report_nudges_to_refs_without_leaking_coords(monkeypatch):
    import interact.dispatch as dispatch

    monkeypatch.setattr(dispatch, "_fmt_cursor", lambda: "default")
    report = dispatch._xy_report("clicked", 137, 451)
    assert "137" not in report and "451" not in report  # still no raw pixels echoed
    assert "ref" in report.lower()  # steers the agent back to detect-then-act by ref


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

