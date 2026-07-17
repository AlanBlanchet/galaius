"""A WM-less nested display hosts utility windows real apps create alongside their UI — Qt's 1x1
"Qt Selection Owner for <app>" — and during a slow startup (a numba/JIT warm) those are the ONLY
windows for tens of seconds. list_windows reporting them made launch_app's poll latch on and
advertise `target="nested:Qt Selection Owner for pagb"` as the drive target while the real window
was still mapping (real PAGB sessions). Tiny utility windows are never drive targets."""

from interact.desktop import NestedBackend


def _bare_backend() -> NestedBackend:
    nb = NestedBackend.__new__(NestedBackend)
    nb.env = {"DISPLAY": ":88"}
    nb.screen_w, nb.screen_h = 400, 400
    return nb


def _fake_x(monkeypatch, nb, windows: dict[int, tuple[str, tuple[int, int]]]):
    """Stub the X queries: windows = {wid: (title, (w, h))}."""
    monkeypatch.setattr(nb, "_search_ids", lambda: [str(w) for w in windows])
    monkeypatch.setattr(nb, "_window_name", lambda wid: windows[int(wid)][0])
    monkeypatch.setattr(nb, "_window_size", lambda wid: windows[int(wid)][1])


def test_tiny_utility_windows_are_not_drive_targets(monkeypatch):
    nb = _bare_backend()
    _fake_x(monkeypatch, nb, {
        5: ("pagb", (1, 1)),  # Qt's bare 1x1 utility window
        7: ("PAGB Reconstruction", (1200, 800)),  # the real app window
        9: ("Qt Selection Owner for pagb", (3, 3)),
    })
    assert nb.list_windows() == [(7, "PAGB Reconstruction")]


def test_only_tiny_windows_means_no_windows_yet(monkeypatch):
    # App mid-startup: only utility windows exist → report NOTHING, so launch_app keeps
    # waiting for the real window (or times out honestly) instead of advertising junk.
    nb = _bare_backend()
    _fake_x(monkeypatch, nb, {
        5: ("pagb", (1, 1)),
        9: ("Qt Selection Owner for pagb", (3, 3)),
    })
    assert nb.list_windows() == []


def test_unknown_geometry_keeps_the_window(monkeypatch):
    # A failed geometry query (0, 0) must never drop a window — only a POSITIVE tiny size does.
    nb = _bare_backend()
    _fake_x(monkeypatch, nb, {3: ("App", (0, 0))})
    assert nb.list_windows() == [(3, "App")]


def test_popup_sized_windows_survive(monkeypatch):
    # Combo/menu popups (override-redirect, a few hundred px) must stay visible to captures.
    nb = _bare_backend()
    _fake_x(monkeypatch, nb, {4: ("popup", (220, 320)), 7: ("App", (1200, 800))})
    assert nb.list_windows() == [(4, "popup"), (7, "App")]
