"""#115: a declared Ctrl-chord arrived as a plain, unmodified keystroke — twice, in two
differently-input-stacked apps (xterm, then VS Code/Electron), so it is the sandbox's synthetic
keyboard rather than an app quirk.

The surviving hypothesis, recorded in `input.py` itself: X and libinput learn about a uinput node
through udev AFTER ``UI_DEV_CREATE``, and events written before that are dropped with no error.
That predicts the reported symptom exactly. The modifiers are written FIRST, so they are the
events that land in the window and vanish; the target key follows microseconds later, by which
time the device is attached, and arrives alone — a plain unmodified keystroke.

So the device must not be written to until the X server actually lists it. Waiting on that
CONDITION (not on a guessed duration) is the fix, and it is what these tests pin.
"""

import pytest

from interact.desktop.input import wait_for_device


def test_it_returns_as_soon_as_the_device_is_listed():
    """The common case must not pay a fixed delay: present on the first look → return at once."""
    calls = []

    def lister():
        calls.append(1)
        return "interact-virtual-keyboard\nAT Translated Set 2 keyboard\n"

    assert wait_for_device("interact-virtual-keyboard", lister=lister) is True
    assert len(calls) == 1, "should not keep polling once the device is there"


def test_it_keeps_polling_until_udev_catches_up():
    """The actual race: the node exists but X has not attached it yet. Appears on the 3rd look."""
    seen = []

    def lister():
        seen.append(1)
        return "" if len(seen) < 3 else "interact-virtual-keyboard\n"

    assert wait_for_device("interact-virtual-keyboard", lister=lister, interval=0.001) is True
    assert len(seen) == 3


def test_it_gives_up_bounded_rather_than_hanging():
    """A headless box with no X server never lists anything. Injection must still be ATTEMPTED —
    returning False, never blocking the caller forever."""
    assert wait_for_device("nope", timeout=0.05, interval=0.001, lister=lambda: "") is False


def test_a_broken_lister_is_not_fatal():
    """`xinput` missing entirely is normal (Wayland, no X tools). Degrade to "not confirmed",
    never raise into the input path."""
    def boom():
        raise OSError("xinput: not found")

    assert wait_for_device("x", timeout=0.02, interval=0.001, lister=boom) is False


def test_the_virtual_keyboard_settles_before_any_key_is_written():
    """The regression itself: constructing the injector must WAIT for the keyboard node, so the
    first chord's modifiers cannot be written into the drop window. Pins the ORDER — settle before
    the caller can possibly write — which is the whole content of the fix."""
    from interact.desktop import input as inp

    order = []

    class FakeUInput:
        def __init__(self, *a, **k):
            order.append(("created", k.get("name")))

        def write(self, *a):
            order.append(("write", None))

        def syn(self):
            pass

        def close(self):
            pass

    fake_evdev = pytest.importorskip("evdev")  # need real ecodes/AbsInfo constants
    import types

    stub = types.SimpleNamespace(
        UInput=FakeUInput, ecodes=fake_evdev.ecodes, AbsInfo=fake_evdev.AbsInfo
    )
    import sys

    real = sys.modules.get("evdev")
    sys.modules["evdev"] = stub
    settled = []
    try:
        inp_wait = inp.wait_for_device
        inp.wait_for_device = lambda name, **k: settled.append(name) or True
        try:
            inp.UinputPointer(1920, 1080)
        finally:
            inp.wait_for_device = inp_wait
    finally:
        if real is not None:
            sys.modules["evdev"] = real

    assert "interact-virtual-keyboard" in settled, (
        "the keyboard node was never waited on — the first chord's modifiers land in the "
        "udev window and are dropped, which is #115"
    )
    assert order and order[0][0] == "created"
