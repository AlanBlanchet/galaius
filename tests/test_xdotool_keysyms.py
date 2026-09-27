"""The nested sandbox drops common keys in SILENCE.

`NestedBackend.key()` forwarded its argument straight to `xdotool key`. But xdotool resolves a
name through XStringToKeysym, which is case-sensitive and knows `Return`/`Escape`/`Up` — NOT
`enter`/`escape`/`up`. Handed one it does not know it prints "No such key name 'enter'. Ignoring
it." and **exits 0**, so `check=True` never fires: the key vanishes and the caller is told it
worked. Verified against a real xdotool on a throwaway display.

That is exactly the failure #115 describes — "silently gets a no-op instead of an error, which is
a confusing failure mode" — and it is the more damaging half, because a silent no-op is
indistinguishable from an app that ignored the key. The uinput backend accepts the lowercase
names, so the SAME `key_press` argument works locally and disappears in the sandbox.

Two fixes, both pinned here: translate the names interact accepts into real keysyms, and never let
xdotool's ignore-and-succeed pass for success.
"""

import pytest

from interact.desktop.input import to_xdotool_key, XdotoolKeyError, check_xdotool_key_output


@pytest.mark.parametrize(
    "given,expected",
    [
        ("enter", "Return"),
        ("escape", "Escape"),
        ("esc", "Escape"),
        ("up", "Up"),
        ("down", "Down"),
        ("tab", "Tab"),
        ("backspace", "BackSpace"),
        ("delete", "Delete"),
        ("space", "space"),
        ("pageup", "Prior"),
        ("pagedown", "Next"),
    ],
)
def test_the_names_interact_accepts_become_real_keysyms(given, expected):
    assert to_xdotool_key(given) == expected


def test_a_chord_keeps_its_modifiers_and_translates_only_the_key():
    """The modifiers are xdotool's own aliases and already work; only the final key needs mapping."""
    assert to_xdotool_key("ctrl+shift+enter") == "ctrl+shift+Return"
    assert to_xdotool_key("ctrl+shift+p") == "ctrl+shift+p"


def test_a_name_that_is_already_a_keysym_is_left_alone():
    """Never mangle a caller who knows X's own vocabulary."""
    assert to_xdotool_key("Return") == "Return"
    assert to_xdotool_key("F1") == "F1"


def test_case_is_not_load_bearing_for_the_caller():
    assert to_xdotool_key("ENTER") == "Return"


def test_a_letter_stays_lowercase():
    """`a` is a keysym; `A` means shift+a to X. Passing a bare letter through unchanged keeps
    type-a-letter behaving the way every caller already expects."""
    assert to_xdotool_key("a") == "a"


def test_xdotools_ignore_and_succeed_is_turned_into_a_real_error():
    """The whole point: exit code 0 with this on stderr is a DROPPED key, not a success."""
    with pytest.raises(XdotoolKeyError) as e:
        check_xdotool_key_output("zzz", "(symbol) No such key name 'zzz'. Ignoring it.")
    assert "zzz" in str(e.value)


def test_ordinary_output_is_not_mistaken_for_a_failure():
    check_xdotool_key_output("ctrl+a", "")  # must not raise


@pytest.mark.parametrize(
    "given,expected",
    [
        # The DOM-style vocabulary the browser side speaks, which `window.py` used to map with a
        # second, case-sensitive table of its own. One translation now serves both backends.
        ("Enter", "Return"),
        ("ArrowDown", "Down"),
        ("ArrowUp", "Up"),
        ("Backspace", "BackSpace"),
        ("Control+a", "ctrl+a"),
        ("Control+Shift+ArrowUp", "ctrl+shift+Up"),
        ("Meta+s", "super+s"),
        ("Cmd+s", "super+s"),
        ("Option+f", "alt+f"),
    ],
)
def test_the_dom_vocabulary_maps_too(given, expected):
    assert to_xdotool_key(given) == expected


def test_the_two_vocabularies_agree():
    """`enter` and `Enter` are the same key. They diverged before: one backend mapped the DOM
    spelling, neither mapped the lowercase one, and the lowercase one was dropped in silence."""
    assert to_xdotool_key("enter") == to_xdotool_key("Enter")
    assert to_xdotool_key("ctrl+arrowup") == to_xdotool_key("Control+ArrowUp")
