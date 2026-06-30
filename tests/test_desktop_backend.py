import pytest

from interact.desktop_backend import ABS_MAX, NestedBackend, screen_to_abs


@pytest.mark.parametrize(
    "clicks, horizontal, expected",
    [
        (2, False, ["4", "4"]),            # vertical up  → button 4 ×2
        (-3, False, ["5", "5", "5"]),      # vertical down → button 5 ×3
        (2, True, ["7", "7"]),             # horizontal right → button 7 ×2
        (-1, True, ["6"]),                 # horizontal left  → button 6 ×1
    ],
    ids=["up", "down", "right", "left"],
)
def test_nested_scroll_emits_axis_button(clicks, horizontal, expected):
    """The nested sandbox must emit the X wheel button for the requested AXIS — vertical 4/5,
    horizontal 6/7. Horizontal scroll silently fell through to a vertical button, so a Flutter
    horizontal carousel never advanced (#54)."""
    nb = NestedBackend.__new__(NestedBackend)
    calls: list[tuple] = []
    nb._xdotool = lambda *a: calls.append(a)
    nb.scroll(clicks, horizontal=horizontal)
    assert [a[1] for a in calls] == expected
    assert all(a[0] == "click" for a in calls)


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
