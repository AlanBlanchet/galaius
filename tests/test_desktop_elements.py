"""DesktopWindow/DesktopElement model: construction, formatting, and the per-window VLM
detection cache.

Covers the plain value-object behaviour (area, key mapping, listing format, IoU-free geometry
helpers) plus the cache that accumulates detections across calls and resets on page-content
change — a stale/accumulated cache is the class of bug behind #57.
"""

import io

import pytest
from PIL import Image as PILImage
from unittest.mock import AsyncMock, MagicMock, patch

from interact.desktop import CoordTransform, DesktopElement, DesktopWindow



@pytest.mark.asyncio
async def test_detection_discards_prior_screen_when_content_changes(monkeypatch):
    """A single-window app (Flutter/aino) keeps ONE window title across every screen, so re-detecting
    after navigating must DISCARD the prior screen's refs — not union them onto the new screenshot.
    Regression: home-screen refs (1..8) piled onto the quiz screen because the cache keyed off the
    (unchanging) window title."""
    import interact.desktop as desktop
    import interact.vision.detect as detect

    def png(color):
        b = io.BytesIO()
        PILImage.new("RGB", (412, 915), color).save(b, "PNG")
        return b.getvalue()

    home_el = [DesktopElement(index=1, role="button", name="Quiz du jour", x=10, y=300, w=180, h=120)]
    quiz_el = [DesktopElement(index=1, role="button", name="le Tibre", x=10, y=600, w=380, h=60)]
    captures = iter([png((235, 235, 235)), png((20, 20, 60))])  # home, then a different screen

    win = MagicMock(wid=778899, w=412, h=915)
    win.name = "aino"  # NOT a MagicMock(name=) kwarg — that sets the mock's repr, not the attr
    win.capture = lambda: next(captures)
    monkeypatch.setattr(detect, "_desktop_context", lambda w: "ctx")
    monkeypatch.setattr(detect.CoordTransform, "from_xprop", staticmethod(lambda wid: CoordTransform()))
    monkeypatch.setattr(detect.CoordTransform, "store", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(detect.Debug, "save", lambda *a, **k: None)
    monkeypatch.setattr(detect, "_vlm_detect_elements",
                        AsyncMock(side_effect=[(home_el, 0.1, "", "vlm"), (quiz_el, 0.1, "", "vlm")]))
    desktop._element_cache.pop(778899, None)
    desktop._page_sig.pop(778899, None)

    _, first, *_ = await detect._detect_desktop_elements(win, method="vlm")
    _, second, *_ = await detect._detect_desktop_elements(win, method="vlm")
    assert [e.name for e in first] == ["Quiz du jour"]
    assert [e.name for e in second] == ["le Tibre"], "prior screen's elements were not discarded"


def test_page_signature_tracks_content_not_identity():
    """The page key must be deterministic, change with screen CONTENT, and never raise — it errs
    toward resetting (a new key) rather than ever keeping stale refs."""
    import interact.vision.detect as detect

    def png(color):
        b = io.BytesIO()
        PILImage.new("RGB", (400, 400), color).save(b, "PNG")
        return b.getvalue()

    home, quiz = png((230, 230, 230)), png((20, 20, 60))
    assert detect._page_signature(home) == detect._page_signature(home)  # deterministic
    assert detect._page_signature(home) != detect._page_signature(quiz)  # content change → new key
    assert detect._page_signature(b"not a png")  # bad bytes → a hash, never an exception


def test_area_computed():
    win = DesktopWindow(name="test", wid=1, w=800, h=600, x=0, y=0)
    assert win.area == 480000


def test_window_listing_format():
    windows = [
        DesktopWindow(name="Zed", wid=2, w=1920, h=1080, x=0, y=0),
        DesktopWindow(name="Alacritty", wid=1, w=800, h=600, x=10, y=10),
    ]
    result = DesktopWindow.listing(windows)
    lines = result.split("\n")
    assert len(lines) == 2
    assert lines[0] == "  Alacritty (800x600, wid:1)"  # wid shown for exact targeting (#5)
    assert lines[1] == "  Zed (1920x1080, wid:2)"


def test_window_listing_empty():
    assert DesktopWindow.listing([]) == ""


def test_desktop_element_center():
    el = DesktopElement(index=1, x=100, y=200, w=80, h=40, role="button", name="OK")
    assert el.center_x == 140
    assert el.center_y == 220


def test_map_key_single():
    assert DesktopWindow.map_key("Enter") == "Return"
    assert DesktopWindow.map_key("ArrowDown") == "Down"
    assert DesktopWindow.map_key("Tab") == "Tab"
    assert DesktopWindow.map_key("a") == "a"


def test_map_key_combo():
    assert DesktopWindow.map_key("Control+a") == "ctrl+a"
    assert DesktopWindow.map_key("Control+Shift+ArrowUp") == "ctrl+shift+Up"
    assert DesktopWindow.map_key("Alt+F4") == "alt+F4"


def test_format_desktop_elements():
    elements = [
        DesktopElement(index=1, x=10, y=20, w=100, h=30, role="button", name="Save"),
        DesktopElement(index=2, x=120, y=20, w=100, h=30, role="button", name="Cancel"),
    ]
    result = DesktopElement.format_list(elements)
    # LLM-facing output: role/name only — no pixel coords (agents reference by index).
    assert "[1] button: 'Save'" in result
    assert "[2] button: 'Cancel'" in result
    assert "100x30" not in result
    assert "at 10,20" not in result


@pytest.mark.parametrize(
    "response, count",
    [
        ('elements: [{"role":"button","name":"OK","x":100,"y":200,"w":80,"h":40}]', 1),
        ("No elements found in this image.", 0),  # no JSON → None
        ("[{invalid json}]", 0),  # malformed → None
        ('[{"role":"b","name":"OK","x":10,"y":20,"w":30,"h":40},{"bad":true}]', 1),  # skips bad entry
    ],
    ids=["valid", "no-json", "malformed", "partial"],
)
def test_parse_vlm_elements_count(response, count):
    els = DesktopElement.parse_vlm(response)
    assert (len(els) if els else 0) == count


def test_parse_vlm_elements_fields():
    el = DesktopElement.parse_vlm(
        '[{"role":"button","name":"OK","x":100,"y":200,"w":80,"h":40}]'
    )[0]
    assert (el.role, el.name, el.x, el.center_x) == ("button", "OK", 100, 140)


def test_ref_to_index():
    assert DesktopElement.ref_to_index("e0") == 0
    assert DesktopElement.ref_to_index("e42") == 42
    assert DesktopElement.ref_to_index("e999") == 999




def test_store_and_get_element():
    elements = [
        DesktopElement(index=1, x=10, y=20, w=30, h=40, role="button", name="OK"),
        DesktopElement(index=2, x=50, y=60, w=70, h=80, role="link", name="Help"),
    ]
    DesktopElement.store(999, elements)
    assert DesktopElement.get_by_index(999, 1) == elements[0]
    assert DesktopElement.get_by_index(999, 2) == elements[1]


def test_get_element_invalid_index():
    DesktopElement.store(888, [])
    assert DesktopElement.get_by_index(888, 1) is None
    assert DesktopElement.get_by_index(777, 5) is None



def test_merge_into_accumulates_within_page_and_clears_on_change():
    """Detections accumulate (refs add up) while the page signature is stable, and clear
    when it changes — the per-window ref session behaviour."""
    from interact.desktop import DesktopElement, _element_cache, _page_sig

    wid = 987654
    _element_cache.pop(wid, None)
    _page_sig.pop(wid, None)

    def el(x: int, name: str) -> DesktopElement:
        return DesktopElement.from_vlm_dict({"x": x, "y": 0, "w": 10, "h": 10, "role": "button", "name": name}, 1)

    first = DesktopElement.merge_into(wid, [el(0, "a")], "page A")
    assert [e.name for e in first] == ["a"]

    # same page → a second/targeted detect ADDS to the existing refs (non-overlapping), re-indexed
    second = DesktopElement.merge_into(wid, [el(100, "b")], "page A")
    assert [e.name for e in second] == ["a", "b"]
    assert [e.index for e in second] == [1, 2]
    assert DesktopElement.get_by_index(wid, 2).name == "b"

    # page change → stale refs cleared, only the new detection remains
    third = DesktopElement.merge_into(wid, [el(0, "c")], "page B")
    assert [e.name for e in third] == ["c"]

    _element_cache.pop(wid, None)
    _page_sig.pop(wid, None)


# --- #57: force-invalidate the desktop element cache ----------------------------------------


def test_desktop_element_cache_invalidate_clears_refs_and_signature():
    """#57: a stale/accumulated cache for a nested window must be force-clearable, so the next
    detection starts empty and returns ONLY the live frame's refs."""
    from interact.desktop import DesktopElement, _element_cache, _page_sig

    wid = 4242
    _element_cache[wid] = ["stale-ref"]
    _page_sig[wid] = "old-signature"
    DesktopElement.invalidate(wid)
    assert wid not in _element_cache and wid not in _page_sig
    assert DesktopElement.cached(wid) is None
    DesktopElement.invalidate(wid)  # idempotent — clearing an empty cache never raises


