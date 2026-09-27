"""Resolving a desktop window from an agent-given target string: exact title beats a larger
partial match, an ambiguous partial lists its candidates, a lone editor/terminal-pattern partial
match refuses to silently guess, and the window id is the stable fallback selector once no title
is unique.
"""

import pytest

from interact import server as srv
from interact.desktop import DesktopWindow


def _all(*windows):
    return classmethod(lambda cls, *a, **k: list(windows))


def test_exact_title_wins_over_a_larger_partial_match(monkeypatch):
    small = DesktopWindow(name="aino", wid=1, x=0, y=0, w=50, h=50)
    ide = DesktopWindow(name="aino - Visual Studio Code", wid=2, x=0, y=0, w=2000, h=1200)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide, small))
    assert srv._find_desktop_window("aino") is small  # exact beats the bigger IDE window


def test_ambiguous_partial_matches_error_lists_candidates(monkeypatch):
    a = DesktopWindow(name="Chrome — Gmail", wid=1, x=0, y=0, w=100, h=100)
    b = DesktopWindow(name="Chrome — GitHub", wid=2, x=0, y=0, w=100, h=100)
    monkeypatch.setattr(DesktopWindow, "all", _all(a, b))
    out = srv._find_desktop_window("Chrome")
    assert isinstance(out, str) and "Gmail" in out and "GitHub" in out
    assert "2" in out  # tells the agent how many matched, so it can disambiguate


def test_single_partial_match_is_returned(monkeypatch):
    only = DesktopWindow(name="aino - Quiz", wid=1, x=0, y=0, w=100, h=100)
    monkeypatch.setattr(DesktopWindow, "all", _all(only))
    assert srv._find_desktop_window("aino") is only


def test_sole_editor_window_partial_match_is_not_silently_driven(monkeypatch):
    """10x in client logs: target='aino' matched ONLY the IDE window ('shared.rs - aino - Visual
    Studio Code') because the app itself ran in the sandbox — and the agent then typed into the
    user's editor. A lone PARTIAL match with an editor/terminal-pattern title needs explicit
    targeting (exact title or wid:), never a silent guess."""
    ide = DesktopWindow(name="shared.rs - aino - Visual Studio Code", wid=7, x=0, y=0, w=2000, h=1200)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide))
    out = srv._find_desktop_window("aino")
    assert isinstance(out, str) and "wid:7" in out
    assert "nested" in out  # hints the app may be in the sandbox instead


def test_exact_editor_title_still_resolves(monkeypatch):
    """Explicitly naming the editor window (exact title) is intentional — never blocked."""
    ide = DesktopWindow(name="shared.rs - aino - Visual Studio Code", wid=7, x=0, y=0, w=2000, h=1200)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide))
    assert srv._find_desktop_window("shared.rs - aino - Visual Studio Code") is ide


# --- #5 part 2: when no title is unique (an app titled "aino" is a substring of the IDE's
#     "aino - Visual Studio Code"), the window id is the only stable selector. ---


def test_listing_includes_window_id():
    app = DesktopWindow(name="aino", wid=29360135, x=0, y=0, w=464, h=1014)
    assert "wid:29360135" in DesktopWindow.listing([app])  # the id the user can copy to target


def test_target_by_window_id_selects_exactly(monkeypatch):
    app = DesktopWindow(name="aino", wid=29360135, x=0, y=0, w=464, h=1014)
    ide = DesktopWindow(name="aino - Visual Studio Code", wid=12, x=0, y=0, w=1920, h=1080)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide, app))
    assert srv._find_desktop_window("wid:29360135") is app  # decimal
    assert srv._find_desktop_window("wid:0x1c00007") is app  # hex (== 29360135), as xwininfo prints


def test_unknown_window_id_errors_with_listing(monkeypatch):
    app = DesktopWindow(name="aino", wid=29360135, x=0, y=0, w=464, h=1014)
    monkeypatch.setattr(DesktopWindow, "all", _all(app))
    out = srv._find_desktop_window("wid:999")
    assert isinstance(out, str) and "999" in out and "aino" in out
