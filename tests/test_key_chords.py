"""Key chords share one grammar across backends (uinput, pynput) via _parse_chord, and the Linux
uinput path now actually supports them — previously UinputPointer.key did getattr(ecodes,
"KEY_CTRL+A") and raised, so a chord like ctrl+a was broken on the real desktop."""

import pytest

from interact.desktop.backend import UinputPointer, _parse_chord


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
    """Records SYN boundaries as well as writes.

    The original fake swallowed `syn()`, which is exactly why #115 slipped through: the key
    events were in the right ORDER and still arrived wrong, because ordering within one event
    FRAME is not ordering in time. A fake that cannot see frames cannot see the bug.
    """

    def __init__(self):
        self.writes: list[tuple[str, int]] = []
        self.stream: list[tuple[str, int] | str] = []

    def write(self, ev, code, value):
        self.writes.append((code, value))
        self.stream.append((code, value))

    def syn(self):
        self.stream.append("SYN")

    def frames(self) -> list[list[tuple[str, int]]]:
        """The writes grouped into the frames they were actually delivered in."""
        out, current = [], []
        for item in self.stream:
            if item == "SYN":
                if current:
                    out.append(current)
                current = []
            else:
                current.append(item)
        if current:
            out.append(current)
        return out


def _uinput(declared: set[str] | None = None):
    """A pointer with no real uinput device behind it.

    `_declared` is set here rather than defaulted inside the guard: production code reading it
    through a `getattr` default meant the check quietly did nothing wherever the attribute was
    absent — which was most of these tests. The fixture carries the cost of using `__new__`.
    """
    up = UinputPointer.__new__(UinputPointer)  # skip __init__ (no real uinput device)
    up._ecodes = _FakeEcodes()
    up._kbd = _FakeKbd()
    up._declared = declared if declared is not None else _EVERY_KEY
    return up


class _EveryKey(frozenset):
    """The fixture's keyboard declares whatever it is asked for, unless a test says otherwise."""

    def __contains__(self, item) -> bool:
        return True


_EVERY_KEY = _EveryKey()


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


# --- #115: a Ctrl-chord arrived as an unmodified keystroke ---
#
# Reported twice, on two differently-built apps: `key_press("ctrl+shift+p")` against VS Code in
# the sandbox opened nothing, and the same against xterm did nothing — the keys landed with no
# modifier applied. The order of the writes was already correct, so the order was not the problem.
#
# An evdev `syn()` closes an ATOMIC event frame. Writing ctrl-down and p-down before the same syn
# hands the compositor one frame that says "these happened together", and the X server / libinput
# evaluates the keypress against the modifier state it held BEFORE the frame — i.e. no ctrl. The
# modifier has to be its own frame, so it is already latched when the key arrives.


def test_a_modifier_is_latched_in_its_own_frame_before_the_key_arrives():
    up = _uinput()
    up.key("ctrl+shift+p")

    frames = up._kbd.frames()
    assert ("KEY_P", 1) not in frames[0], (
        "the key was delivered in the same frame as the modifiers — the app evaluates it against "
        "the modifier state from BEFORE the frame, so it reads as an unmodified keystroke (#115)"
    )
    assert frames[0] == [("KEY_LEFTCTRL", 1), ("KEY_LEFTSHIFT", 1)], frames[0]
    assert ("KEY_P", 1) in frames[1]


def test_the_modifier_is_still_held_when_the_key_is_released():
    """Releasing ctrl in the same frame as the key-up can register as a bare ctrl tap, which some
    apps bind on its own."""
    up = _uinput()
    up.key("ctrl+a")

    frames = up._kbd.frames()
    release_frame = next(f for f in frames if ("KEY_A", 0) in f)
    assert ("KEY_LEFTCTRL", 0) not in release_frame, "modifier released in the key-up frame"


def test_a_plain_key_costs_only_the_two_frames_hardware_would_send():
    """Down and up are separate frames on a real keyboard; no chord means no extra ones."""
    up = _uinput()
    up.key("a")
    assert up._kbd.frames() == [[("KEY_A", 1)], [("KEY_A", 0)]]


# --- #115, the half that is provable: a key the device never DECLARED is silently discarded ---
#
# A uinput device may only emit key codes it declared at creation. `key()` happily resolves
# "F1" -> KEY_F1 and writes it, the kernel drops it on the floor, and nothing anywhere reports a
# problem — the reporter saw `key_press("F1")` do nothing at all and had no signal pointing at the
# cause. `super+…` has the same hole: _UINPUT_MODIFIERS maps it to KEY_LEFTMETA, which was never
# in the declared set either.


def test_every_modifier_the_grammar_accepts_is_actually_declared():
    from evdev import ecodes

    from interact.desktop.input import _keyboard_codes, _UINPUT_MODIFIERS

    declared = set(_keyboard_codes(ecodes))
    for token, name in _UINPUT_MODIFIERS.items():
        assert getattr(ecodes, name) in declared, (
            f"{token!r} maps to {name}, which the virtual keyboard never declares — the kernel "
            "discards it and the chord silently arrives unmodified"
        )


def test_function_keys_are_declared():
    from evdev import ecodes

    from interact.desktop.input import _keyboard_codes

    declared = set(_keyboard_codes(ecodes))
    missing = [f"KEY_F{i}" for i in range(1, 13) if getattr(ecodes, f"KEY_F{i}") not in declared]
    assert not missing, f"undeclared and therefore silently dropped: {missing}"


def test_an_undeclared_key_fails_loudly_rather_than_doing_nothing():
    """The reporter's actual complaint: 'silently gets a no-op instead of an error'."""
    up = _uinput(declared={"KEY_A"})

    with pytest.raises(ValueError, match="cannot send"):
        up.key("KEY_SYSRQ")


def test_the_portable_backend_also_refuses_a_key_it_cannot_resolve():
    """It used to fall back to the raw token, which pynput TYPES — so a mistyped key name quietly
    wrote itself into the document instead of reporting anything."""
    from interact.desktop.backend import PortableBackend

    class _Key:
        enter = "ENTER"

    b = PortableBackend.__new__(PortableBackend)
    b._Key, b._KEYS = _Key(), {}

    assert b._resolve_key("enter") == "ENTER"
    assert b._resolve_key("x") == "x"
    with pytest.raises(ValueError, match="cannot send"):
        b._resolve_key("f13")


def test_type_text_puts_a_shifted_character_in_ONE_frame():
    """This is the evidence that the chord's frame-splitting is not the fix for #115.

    `type_text` writes shift-down, key-down, key-up, shift-up and a single syn() — exactly the
    "atomic frame" a chord was said to be broken by — and typing capitals is the most exercised
    path in this module. Both cannot be true, so the theory is wrong and the cause is still open.
    Pinned here so nobody re-derives the same wrong explanation from the chord code alone.
    """
    up = _uinput()
    up._char_spec = lambda ch: ("KEY_A", True)
    up.type_text("A")

    assert up._kbd.frames() == [[
        ("KEY_LEFTSHIFT", 1), ("KEY_A", 1), ("KEY_A", 0), ("KEY_LEFTSHIFT", 0),
    ]]


def test_the_guard_cannot_silently_skip_itself():
    """It used to read `getattr(self, "_declared", None)` and do nothing when absent — the shape a
    fixture presses onto production code. A pointer with no declared set must now fail loudly
    rather than wave the key through."""
    up = _uinput()
    del up._declared

    with pytest.raises(AttributeError):
        up.key("a")
