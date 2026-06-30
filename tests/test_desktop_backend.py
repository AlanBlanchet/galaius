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


# --- #61/#62: non-blocking desktop record sessions ------------------------------------------


def test_ffmpeg_grab_args_omit_t_for_a_session_and_include_it_for_a_clip():
    """A session (duration=None) records open-ended (no ``-t``) so it runs until stopped; an
    explicit duration keeps the blocking one-shot clip's ``-t`` for backward compat (#61/#62)."""
    from interact.desktop_backend import _ffmpeg_grab_args

    session = _ffmpeg_grab_args(":99", 0, 0, 412, 780, 12, "/tmp/x.mp4", duration=None)
    assert "-t" not in session
    assert session[-1] == "/tmp/x.mp4" and "x11grab" in session

    clip = _ffmpeg_grab_args(":99", 0, 0, 412, 780, 12, "/tmp/x.mp4", duration=3.0)
    assert clip[clip.index("-t") + 1] == "3.0"


def test_video_session_stop_finalizes_then_reads_and_cleans_up(tmp_path, monkeypatch):
    """stop() sends 'q' on stdin so ffmpeg writes a valid moov atom (a seekable mp4), then reads
    the file and unlinks it. A SIGTERM-only stop would truncate the moov and corrupt the clip."""
    from interact import desktop_backend as db

    out = tmp_path / "rec.mp4"
    out.write_bytes(b"VIDEO")

    class FakeProc:
        def __init__(self):
            self.sent = None

        def communicate(self, input=None, timeout=None):
            self.sent = input
            return (b"", b"")

    fake = FakeProc()
    monkeypatch.setattr(db.subprocess, "Popen", lambda *a, **k: fake)
    s = db._VideoSession(["ffmpeg", "-y"], str(out))
    data = s.stop()
    assert data == b"VIDEO"
    assert fake.sent == b"q"          # graceful finalize, not a kill
    assert not out.exists()           # temp file cleaned up
