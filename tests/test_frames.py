import pytest

from interact.desktop.frames import Frame


@pytest.fixture
def stack():
    """screen → monitor(+1920,+0) → window(+50,+80) → image(window captured @2x for the VLM)."""
    screen = Frame(name="screen")
    monitor = screen.child("monitor1", offset_x=1920, offset_y=0)
    window = monitor.child("window", offset_x=50, offset_y=80)
    # image is the window captured then upscaled 2x (so 1 image px = 0.5 window px)
    image = window.child("image", scale_x=0.5, scale_y=0.5)
    return screen, monitor, window, image


class TestFrame:
    def test_window_to_screen_adds_monitor_and_window_offsets(self, stack):
        _screen, _monitor, window, _image = stack
        # (10,10) in the window sits at monitor (1920+50+10, 80+10)
        assert window.to_root(10, 10) == (1980, 90)

    def test_image_to_window_undoes_resize(self, stack):
        _s, _m, window, image = stack
        # (200,200) in the 2x image → (100,100) in the window
        assert image.convert(200, 200, to=window) == (100, 100)

    def test_image_to_screen_composes_resize_and_offsets(self, stack):
        screen, _m, _w, image = stack
        # (200,200) image → (100,100) window → +offsets → screen
        assert image.convert(200, 200, to=screen) == (1920 + 50 + 100, 80 + 100)

    def test_round_trip_is_identity(self, stack):
        screen, _m, _w, image = stack
        sx, sy = image.convert(123, 45, to=screen)
        assert image.from_root(*screen.to_root(sx, sy)) == pytest.approx((123, 45))

    def test_screen_to_window_subtracts_offsets(self, stack):
        screen, _m, window, _i = stack
        assert screen.convert(1980, 90, to=window) == (10, 10)
