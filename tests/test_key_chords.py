"""Key chords share one grammar across backends (uinput, pynput) via _parse_chord, and the Linux
uinput path now actually supports them — previously UinputPointer.key did getattr(ecodes,
"KEY_CTRL+A") and raised, so a chord like ctrl+a was broken on the real desktop."""

import pytest

from interact.desktop_backend import UinputPointer, _parse_chord


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("a", ([], "a")),
        ("ctrl+a", (["ctrl"], "a")),
        ("ctrl+shift+k", (["ctrl", "shift"], "k")),
        ("Return", ([], "Return")),
    ],
)
def test_parse_chord(spec, expected):
    assert _parse_chord(spec) == expected


class _FakeEcodes:
    EV_KEY = 1

    def __getattr__(self, name):  # KEY_LEFTCTRL, KEY_A, … resolve to their own name
        return name


class _FakeKbd:
    def __init__(self):
        self.writes: list[tuple[str, int]] = []

    def write(self, ev, code, value):
        self.writes.append((code, value))

    def syn(self):
        pass


def _uinput():
    up = UinputPointer.__new__(UinputPointer)  # skip __init__ (no real uinput device)
    up._ecodes = _FakeEcodes()
    up._kbd = _FakeKbd()
    return up


def test_uinput_chord_holds_modifier_then_releases_in_reverse():
    up = _uinput()
    up.key("ctrl+a")
    assert up._kbd.writes == [
        ("KEY_LEFTCTRL", 1),
        ("KEY_A", 1),
        ("KEY_A", 0),
        ("KEY_LEFTCTRL", 0),
    ]


def test_uinput_single_key_unchanged():
    up = _uinput()
    up.key("a")
    assert up._kbd.writes == [("KEY_A", 1), ("KEY_A", 0)]
