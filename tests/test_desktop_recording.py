"""Desktop recording: motion detection, session vs one-shot ffmpeg args, video-session stop,
and the sandbox's private audio sink (#47, #61/#62)."""

from unittest.mock import patch

from interact.desktop import Motion


def test_detect_motion_ffmpeg_failure():
    with patch("interact.desktop.subprocess.run", side_effect=RuntimeError("boom")):
        assert Motion.detect(b"fake video") is True



# --- #61/#62: non-blocking desktop record sessions ------------------------------------------


def test_ffmpeg_grab_args_omit_t_for_a_session_and_include_it_for_a_clip():
    """A session (duration=None) records open-ended (no ``-t``) so it runs until stopped; an
    explicit duration keeps the blocking one-shot clip's ``-t`` for backward compat (#61/#62)."""
    from interact.desktop import _ffmpeg_grab_args

    session = _ffmpeg_grab_args(":99", 0, 0, 412, 780, 12, "/tmp/x.mp4", duration=None)
    assert "-t" not in session
    assert session[-1] == "/tmp/x.mp4" and "x11grab" in session

    clip = _ffmpeg_grab_args(":99", 0, 0, 412, 780, 12, "/tmp/x.mp4", duration=3.0)
    assert clip[clip.index("-t") + 1] == "3.0"


def test_video_session_stop_finalizes_then_reads_and_cleans_up(tmp_path, monkeypatch):
    """stop() sends 'q' on stdin so ffmpeg writes a valid moov atom (a seekable mp4), then
    reads the file and unlinks it. A SIGTERM-only stop would truncate the moov and corrupt
    the clip."""
    from interact.desktop import video as db

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


# --- #47: recordings carry the sandbox's audio ----------------------------------------------


def test_ffmpeg_grab_args_mux_audio_when_a_source_is_given():
    from interact.desktop import _ffmpeg_grab_args

    args = _ffmpeg_grab_args(":99", 0, 0, 412, 780, 12, "/tmp/x.mp4",
                             duration=None, audio_source="interact_99.monitor")
    joined = " ".join(args)
    assert "-f pulse -i interact_99.monitor" in joined
    assert "-c:a" in args  # audio encoded into the mp4
    # video-only stays exactly as before
    silent = _ffmpeg_grab_args(":99", 0, 0, 412, 780, 12, "/tmp/x.mp4", duration=3.0)
    assert "pulse" not in " ".join(silent)


def test_sandbox_audio_sink_is_created_once_and_spawn_routes_apps_into_it(monkeypatch):
    """The sandbox owns a private null sink: launched apps get PULSE_SINK so their audio lands
    there (inaudible, isolated from the user's audio), and recordings read its .monitor (#47)."""
    from interact.desktop import nested as db

    nb = db.NestedBackend.__new__(db.NestedBackend)
    nb.display = ":99"
    nb.env = {"DISPLAY": ":99"}
    nb._audio_module = None
    nb._audio_sink = None
    calls: list[list[str]] = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        class R: returncode, stdout = 0, "42\n"
        return R()

    monkeypatch.setattr(db.subprocess, "run", fake_run)
    sink = nb._ensure_audio_sink()
    assert sink and "99" in sink
    assert any("load-module" in " ".join(c) for c in calls)
    calls.clear()
    assert nb._ensure_audio_sink() == sink and not calls  # created once, cached

    assert nb.env.get("PULSE_SINK") == sink  # spawned apps inherit the sink


def test_audio_sink_failure_degrades_to_video_only(monkeypatch):
    from interact.desktop import nested as db

    nb = db.NestedBackend.__new__(db.NestedBackend)
    nb.display = ":99"
    nb.env = {"DISPLAY": ":99"}
    nb._audio_module = None
    nb._audio_sink = None

    def boom(cmd, **k):
        raise FileNotFoundError("no pactl")

    monkeypatch.setattr(db.subprocess, "run", boom)
    assert nb._ensure_audio_sink() is None  # no crash — recording stays video-only


