"""interact must not drive the editor window that is hosting the caller.

An agent typing a command into its own editor can destroy the process issuing the command —
"Developer: Reload Window" ends the session mid-task, and nothing survives to notice or repair
it. This actually happened: a reload aimed at another window landed in the caller's own, and the
session died.

The blast radius of an action must never include the actor. So a desktop target that resolves to
the caller's own editor window is refused by default, with the reason and the escape hatch named.
"""

import pytest

from interact.desktop.selfguard import is_self_window, self_window_hints


@pytest.fixture(autouse=True)
def _caller(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/project/dev/interact")
    yield


@pytest.mark.parametrize(
    "title",
    [
        "interact - Visual Studio Code",
        "interact — Visual Studio Code",       # em dash, some builds
        "● interact - Visual Studio Code",     # unsaved-changes marker
        "INTERACT - Visual Studio Code",       # case
    ],
)
def test_the_callers_own_editor_window_is_recognised(title):
    assert is_self_window(title) is True, f"{title!r} is the window running this session"


@pytest.mark.parametrize(
    "title",
    [
        "hintconf - Visual Studio Code",
        "AI-4-Alan - Visual Studio Code",
        "interactive-demo - Visual Studio Code",  # merely CONTAINS the word, different project
        "Google Chrome",
        "Xephyr on :99.0",
    ],
)
def test_other_windows_are_not_mistaken_for_it(title):
    assert is_self_window(title) is False


def test_a_caller_with_no_project_dir_claims_no_self_window(monkeypatch):
    # Guessing here would be worse than not guarding: it would block legitimate targets.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr("interact.desktop.selfguard._cwd_name", lambda: "")
    assert self_window_hints() == []
    assert is_self_window("anything - Visual Studio Code") is False


def test_the_hint_is_the_project_name_not_a_path():
    assert "interact" in self_window_hints()
    assert not any("/" in h for h in self_window_hints())


# ── the guard has to be ON the path, not merely available ────────────────────────────────────


def test_run_actions_refuses_the_callers_own_editor(monkeypatch):
    """A helper nobody calls is not a guard. Mutating input aimed at the caller's own editor must
    be refused at the point the action is dispatched.

    The window list is STUBBED: reading the real desktop made this test depend on which windows
    the user happens to have open, and it failed the moment one of them was retitled — a guard
    this important cannot have a pass/fail that moves with someone's editor tabs.
    """
    import asyncio

    import interact.server as srv

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/project/dev/interact")
    # Stub the RESOLUTION, not the desktop: what matters is that a resolved window bearing the
    # caller's own project name is refused.
    class _Win:
        name = "interact - Visual Studio Code"

    monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (_Win(), None, None))
    from interact.actions import KeyPressAction

    out = asyncio.run(
        srv.run_actions(
            [KeyPressAction(key="ctrl+shift+p")],
            target="interact - Visual Studio Code",
        )
    )
    assert "REFUSED" in out and "hosting THIS session" in out


def test_reading_the_callers_own_window_is_NOT_refused():
    """Looking is harmless and useful — the guard is about INPUT. Blocking a read would make the
    agent blind to its own editor for no safety gain."""
    from interact.actions import HoverAction
    from interact.desktop.selfguard import refusal_for

    assert refusal_for("interact - Visual Studio Code", [HoverAction(x=1, y=1)],
                       allow_self=False) is None


def test_input_at_the_callers_own_window_IS_refused():
    from interact.actions import KeyPressAction
    from interact.desktop.selfguard import refusal_for

    out = refusal_for("interact - Visual Studio Code", [KeyPressAction(key="ctrl+r")],
                      allow_self=False)
    assert out and "REFUSED" in out and "hosting THIS session" in out


def test_one_mutating_action_in_a_batch_is_enough_to_refuse():
    """A read followed by a keystroke is still a keystroke into the caller's own editor."""
    from interact.actions import HoverAction, KeyPressAction
    from interact.desktop.selfguard import refusal_for

    batch = [HoverAction(x=1, y=1), KeyPressAction(key="ctrl+r")]
    assert refusal_for("interact - Visual Studio Code", batch, allow_self=False)


def test_allow_self_is_the_deliberate_escape_hatch():
    from interact.actions import KeyPressAction
    from interact.desktop.selfguard import refusal_for

    assert refusal_for("interact - Visual Studio Code", [KeyPressAction(key="ctrl+r")],
                       allow_self=True) is None


def test_another_window_is_never_refused():
    from interact.actions import KeyPressAction
    from interact.desktop.selfguard import refusal_for

    assert refusal_for("some-other-app", [KeyPressAction(key="ctrl+r")], allow_self=False) is None


# ── typing must land where it was aimed ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_typing_verifies_the_window_actually_has_focus(monkeypatch):
    """`windowactivate` is asynchronous and best-effort — it can lose a race or be refused by the
    WM. Typing anyway sends the keystrokes to WHATEVER holds focus, which is how a reload aimed
    at one window landed in another and killed the session. Verify, then type."""
    from interact.desktop import DesktopWindow

    win = DesktopWindow(name="target", wid=4242, w=800, h=600, x=0, y=0)
    typed: list[str] = []

    async def fake_run(*args):
        if args[:2] == ("xdotool", "getwindowfocus"):
            return "9999"  # a DIFFERENT window ended up focused
        if args[1] == "type":
            typed.append(args[-1])
        return ""

    monkeypatch.setattr(win, "_run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        await win.type_text("Developer: Reload Window")
    assert "focus" in str(exc.value).lower()
    assert typed == [], "keystrokes were sent to a window that was not the target"


@pytest.mark.asyncio
async def test_typing_proceeds_when_focus_landed(monkeypatch):
    from interact.desktop import DesktopWindow

    win = DesktopWindow(name="target", wid=4242, w=800, h=600, x=0, y=0)
    typed: list[str] = []

    async def fake_run(*args):
        if args[:2] == ("xdotool", "getwindowfocus"):
            return "4242"  # the intended window
        if args[1] == "type":
            typed.append(args[-1])
        return ""

    monkeypatch.setattr(win, "_run", fake_run)
    await win.type_text("hello")
    assert typed == ["hello"]
