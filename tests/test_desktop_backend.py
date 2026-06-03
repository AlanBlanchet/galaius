import pytest

from interact.desktop_backend import ABS_MAX, screen_to_abs


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
