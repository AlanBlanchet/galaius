"""DesktopWindow input commands: click, type, key, scroll, drag, hover.

Every action is asserted as the exact xdotool invocation it must issue (focus verified before a
keystroke, one delayed click burst for a wheel/double-click, axis threaded through to the backend
for horizontal scroll on a sandboxed window). `screen_to_abs` and its absolute-coordinate range
clamp are the same input-mapping concern for the uinput backend.

#12/#13 — delivery fidelity for GTK/Flutter toolkits: scroll must focus the window before the
first wheel event (silently dropped otherwise), a drag must be a continuous time-spread pointer
path (a couple of teleports is not recognised as a drag/fling), and keyboard input on the sandbox
backend must focus the window's own resolved wid, never re-search by title (#25).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interact.desktop import ABS_MAX, DesktopWindow, NestedBackend, _DRAG_STEPS, screen_to_abs
from interact.desktop.backend import PortableBackend
from tests.support.desktop import desktop_window


@pytest.fixture
def mock_run():
    with (
        patch("interact.desktop.DesktopWindow._run", new_callable=AsyncMock) as m,
        patch(
            "interact.desktop.DesktopWindow.active_id",
            new_callable=AsyncMock,
            return_value="60818159",
        ),
    ):
        yield m


@pytest.fixture
def _win():
    return desktop_window()


@pytest.mark.parametrize("button", [1, 3], ids=["left", "right"])
@pytest.mark.parametrize(
    "count, repeat",
    # #116: a double-click is ONE xdotool process with an explicit inter-click delay, so the two
    # presses land evenly spaced inside the toolkit's double-click interval.
    [(1, ()), (2, ("--repeat", "2", "--delay", "60"))],
    ids=["single", "double"],
)
@pytest.mark.asyncio
async def test_desktop_click_commands(mock_run, _win, button, count, repeat):
    await _win.click(50, 100, button=button, count=count)
    assert mock_run.call_count == 3
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "50", "100")
    mock_run.assert_any_call("xdotool", "click", *repeat, str(button))


def test_portable_backend_double_click_uses_pynputs_own_count():
    """macOS recognises a double-click by the click-state pynput stamps on each event ONLY inside
    its own multi-click call — two separate press/release pairs never read as one there (#116)."""
    be = PortableBackend.__new__(PortableBackend)  # no mss/pynput needed to exercise the call shape
    be._mouse, be._Button = MagicMock(), SimpleNamespace(left="L", right="R", middle="M")
    be.click(30, 40, "right", count=2)
    assert be._mouse.position == (30, 40)
    be._mouse.click.assert_called_once_with("R", 2)


@pytest.mark.asyncio
async def test_desktop_type_commands(mock_run, _win):
    await _win.type_text("hello world")
    assert mock_run.call_count == 4
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "windowfocus", "--sync", "123")
    # Focus is VERIFIED before any keystroke: an async activate can lose the race, and keys go to
    # whatever is focused. Sending them blind is how a command lands in the wrong window.
    mock_run.assert_any_call("xdotool", "getwindowfocus")
    mock_run.assert_any_call(
        "xdotool",
        "type",
        "--clearmodifiers",
        "--delay",
        "12",
        "--",
        "hello world",
    )


@pytest.mark.asyncio
async def test_desktop_key_commands(mock_run, _win):
    await _win.press_key("Enter")
    assert mock_run.call_count == 4
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "windowfocus", "--sync", "123")
    # Focus is VERIFIED before any keystroke: an async activate can lose the race, and keys go to
    # whatever is focused. Sending them blind is how a command lands in the wrong window.
    mock_run.assert_any_call("xdotool", "getwindowfocus")
    mock_run.assert_any_call(
        "xdotool",
        "key",
        "--clearmodifiers",
        "--",
        "Return",
    )


@pytest.mark.asyncio
async def test_desktop_key_combo(mock_run, _win):
    await _win.press_key("Control+a")
    assert mock_run.call_count == 4
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "windowfocus", "--sync", "123")
    # Focus is VERIFIED before any keystroke: an async activate can lose the race, and keys go to
    # whatever is focused. Sending them blind is how a command lands in the wrong window.
    mock_run.assert_any_call("xdotool", "getwindowfocus")
    mock_run.assert_any_call(
        "xdotool",
        "key",
        "--clearmodifiers",
        "--",
        "ctrl+a",
    )


@pytest.mark.parametrize(
    "direction, clicks, button",
    [("down", 3, "5"), ("up", 2, "4"), ("left", 2, "6"), ("right", 2, "7")],
    ids=["down", "up", "left", "right"],
)
@pytest.mark.asyncio
async def test_desktop_scroll(mock_run, _win, direction, clicks, button):
    await _win.scroll(50, 100, direction, clicks)
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "50", "100")
    scroll_calls = [c for c in mock_run.call_args_list if "click" in c.args]
    assert len(scroll_calls) == clicks
    for c in scroll_calls:
        assert c.args == ("xdotool", "click", "--window", "123", button)


@pytest.mark.parametrize(
    "direction, amount, expected",
    [
        ("up", 3, (3, False)),
        ("down", 2, (-2, False)),
        ("right", 4, (4, True)),     # horizontal must NOT collapse to a vertical button (#54)
        ("left", 1, (-1, True)),
    ],
    ids=["up", "down", "right", "left"],
)
@pytest.mark.asyncio
async def test_backend_scroll_threads_axis(direction, amount, expected):
    """A scroll on a sandbox-bound window must reach the backend with the right axis + sign. Before
    the fix, left/right fell into the vertical branch (clicks=-amount, no axis), so a Flutter
    horizontal carousel never advanced (#54)."""
    win = DesktopWindow(name="aino", wid=7, w=412, h=915, x=0, y=0)
    calls: list[tuple] = []

    class FakeBackend:
        def move(self, x, y):
            pass

        def focus_wid(self, wid):
            pass

        def scroll(self, clicks, horizontal=False):
            calls.append((clicks, horizontal))

    win._backend = FakeBackend()
    await win.scroll(100, 200, direction, amount)
    assert calls == [expected]


@pytest.mark.asyncio
async def test_desktop_drag_commands(mock_run, _win):
    await _win.drag(0, 0, 100, 100, steps=5)
    # activate + mousemove start + mousedown + 5 intermediate mousemoves + mouseup
    # + one settle move at the drop point, which releases the webview's mouse capture (#136) = 10
    assert mock_run.call_count == 10
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "0", "0")
    mock_run.assert_any_call("xdotool", "mousedown", "1")
    mock_run.assert_any_call("xdotool", "mouseup", "1")
    # verify intermediate coords (linear interpolation)
    for i in range(1, 6):
        expected_x = str(100 * i // 5)
        expected_y = str(100 * i // 5)
        mock_run.assert_any_call(
            "xdotool", "mousemove", "--window", "123", expected_x, expected_y
        )


@pytest.mark.asyncio
async def test_desktop_hover_commands(mock_run, _win):
    await _win.hover(200, 300)
    assert mock_run.call_count == 2
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "200", "300")


@pytest.mark.asyncio
async def test_mouse_no_focus_stealing(mock_run):
    w = DesktopWindow(name="test", wid=1, w=800, h=600, x=0, y=0)
    await w.scroll(0, 0, "down", 1)
    await w.drag(0, 0, 1, 1, steps=1)
    await w.hover(0, 0)
    restore_calls = [
        c
        for c in mock_run.call_args_list
        if c.args == ("xdotool", "windowactivate", "60818159")
    ]
    assert len(restore_calls) == 0


@pytest.mark.asyncio
async def test_keyboard_targets_window(mock_run):
    w = DesktopWindow(name="test", wid=1, w=800, h=600, x=0, y=0)
    await w.type_text("x")
    await w.press_key("a")
    activate_calls = [c for c in mock_run.call_args_list if "windowactivate" in c.args]
    focus_calls = [c for c in mock_run.call_args_list if "windowfocus" in c.args]
    assert len(activate_calls) == 2
    assert len(focus_calls) == 2



@pytest.mark.asyncio
async def test_run_raises_on_failure():
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"some error")
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(RuntimeError, match="xdotool failed"):
            await DesktopWindow._run("xdotool", "fake", "cmd")





@pytest.mark.parametrize(
    "clicks, horizontal, button, repeat",
    [
        (2, False, "4", 2),    # vertical up  → button 4 ×2
        (-3, False, "5", 3),   # vertical down → button 5 ×3
        (2, True, "7", 2),     # horizontal right → button 7 ×2
        (-1, True, "6", 1),    # horizontal left  → button 6 ×1
    ],
    ids=["up", "down", "right", "left"],
)
def test_nested_scroll_emits_axis_button(clicks, horizontal, button, repeat):
    """The nested sandbox must emit the X wheel button for the requested AXIS — vertical 4/5,
    horizontal 6/7. Horizontal scroll silently fell through to a vertical button, so a Flutter
    horizontal carousel never advanced (#54).

    The clicks go out as ONE xdotool invocation with an explicit inter-click delay, not N
    racing processes: an unspaced burst of press/release pairs was the shape behind the
    intermittent wrong-event side effects of repeated scrolls (#88)."""
    nb = NestedBackend.__new__(NestedBackend)
    calls: list[tuple] = []
    nb._xdotool = lambda *a: calls.append(a)
    nb.scroll(clicks, horizontal=horizontal)
    assert len(calls) == 1, "a wheel burst must be one ordered, delayed sequence"
    args = calls[0]
    assert args[0] == "click" and args[-1] == button
    assert args[args.index("--repeat") + 1] == str(repeat)
    assert int(args[args.index("--delay") + 1]) > 0


def test_nested_scroll_with_no_clicks_does_nothing():
    nb = NestedBackend.__new__(NestedBackend)
    calls: list[tuple] = []
    nb._xdotool = lambda *a: calls.append(a)
    nb.scroll(0)
    assert calls == []


class TestScreenToAbs:
    @pytest.mark.parametrize(
        "x, y, expected",
        [
            (0, 0, (0, 0)),
            (1920, 1080, (ABS_MAX, ABS_MAX)),
            (960, 540, (round(ABS_MAX / 2), round(ABS_MAX / 2))),
        ],
    )
    def test_maps_screen_px_into_abs_range(self, x, y, expected):
        assert screen_to_abs(x, y, 1920, 1080) == expected

    def test_clamps_out_of_bounds(self):
        # off-screen detections must not fling the absolute pointer past the edges
        assert screen_to_abs(5000, -10, 1920, 1080) == (ABS_MAX, 0)

    def test_zero_screen_is_safe(self):
        assert screen_to_abs(10, 10, 0, 0) == (0, 0)



def _aino_window() -> DesktopWindow:
    return DesktopWindow(name="aino", wid=4242, x=0, y=0, w=400, h=800)


@pytest.mark.asyncio
async def test_scroll_focuses_window_before_wheel(monkeypatch):
    """A Flutter/GTK window silently drops a synthetic wheel unless the window holds focus; scroll
    must raise+focus before sending wheel buttons (#12/#13). Clicks worked already because they
    self-focus — scroll didn't."""
    win = _aino_window()
    calls: list[str] = []
    monkeypatch.setattr(win, "_activate", AsyncMock(side_effect=lambda: calls.append("activate")))
    monkeypatch.setattr(win, "_focus", AsyncMock(side_effect=lambda: calls.append("focus")))
    monkeypatch.setattr(win, "_mousemove", AsyncMock(side_effect=lambda x, y: calls.append("move")))
    monkeypatch.setattr(win, "_clickbtn", AsyncMock(side_effect=lambda b: calls.append(f"wheel{b}")))

    await win.scroll(200, 400, "down", amount=3)

    assert calls.index("focus") < calls.index("wheel5"), "must focus before the first wheel event"
    assert calls.count("wheel5") == 3, "one wheel-down per unit of amount"


@pytest.mark.asyncio
async def test_backend_keyboard_focuses_resolved_wid_not_title():
    """On the sandbox backend, keyboard input must focus the EXACT window this DesktopWindow
    resolved to (its wid) — the same window clicks act on — never re-search by title, which can
    pick a hidden helper window so 'clicks work but typing doesn't' (#25)."""
    calls: list[tuple] = []

    class FakeBackend:
        def focus_wid(self, wid):
            calls.append(("focus_wid", wid))

        def focus(self, name):
            calls.append(("focus_by_name", name))

        def type_text(self, text):
            calls.append(("type", text))

    win = DesktopWindow(name="Payload", wid=99, x=0, y=0, w=400, h=800)
    win._backend = FakeBackend()
    await win.type_text("hi")

    assert ("focus_wid", 99) in calls
    assert all(c[0] != "focus_by_name" for c in calls), "must not re-resolve focus by title"
    assert calls.index(("focus_wid", 99)) < calls.index(("type", "hi")), "focus before typing"


@pytest.mark.asyncio
async def test_drag_emits_fine_time_spread_path(monkeypatch):
    """Flutter recognises a drag/fling only from a continuous, time-spread pointer path — many small
    moves (float-interpolated, not pixel-quantized) with per-step delays, not a couple of teleports
    (#12/#13)."""
    win = _aino_window()
    moves: list[tuple[int, int]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(win, "_activate", AsyncMock())
    monkeypatch.setattr(win, "_run", AsyncMock())
    monkeypatch.setattr(win, "_mousemove", AsyncMock(side_effect=lambda x, y: moves.append((x, y))))

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr("interact.desktop.asyncio.sleep", fake_sleep)
    await win.drag(200, 700, 200, 100, steps=_DRAG_STEPS)

    step_moves = moves[1:]  # first move is the initial positioning
    assert len(step_moves) >= 24, f"drag must be many small steps, got {len(step_moves)}"
    ys = [y for _, y in step_moves]
    assert ys == sorted(ys, reverse=True), "y must descend smoothly toward the target"
    assert len(set(ys)) >= 20, "float interpolation → distinct intermediate points, not quantized dupes"
    assert any(0 < d < 0.05 for d in sleeps), "per-step delay spreads the path over time for Flutter"



