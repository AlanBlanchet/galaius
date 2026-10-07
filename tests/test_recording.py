"""Recording: session lifecycle, viewport fidelity, storage-state carryover, fps/metadata.

- Recording recovery: a slow reload after `record(start=True)` used to brick recording for the
  rest of the session (`_recording_dir` set BEFORE re-navigating the fresh context); a failed
  start now leaves the session able to try again; the return says WHERE the page actually
  landed; and every context swap carries the WHOLE storage_state (cookies AND localStorage —
  #123).
- #110 record fidelity: the recording is sized to the emulated viewport, rebuilt around the
  ACTIVE tab (not `pages[0]`), and keeps the forced media features (`prefers-reduced-motion`,
  `color-scheme`) that `emulate_device` applied.
- Record fps + metadata (#122/#133/#135): the requested fps is honoured end to end and dropped
  on a failed start; `_record_metadata` reads the ENCODED rate/duration from ffprobe rather than
  trusting the requested sampling, and never invents a rate when the probe fails or is malformed.
- Record sessions (#61/#62): `record(start=True)` on desktop/browser opens a NON-blocking
  session instead of the old forced fixed-duration clip; stopping with no session open explains
  itself instead of crashing; an explicit `duration=` stays a blocking one-shot clip for backward
  compat; the sampling caveat reports the actual largest frame gap.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from galaius.browser import BrowserManager
from galaius.vision import VLMResult
from tests.support import browser_manager, ready_or_skip


@pytest.fixture
def srv():
    import galaius.server as _srv
    from galaius.server import breaker
    from unittest.mock import patch

    breaker.clear()
    _srv.config.component_criteria = "cap.gui_grounding"
    with patch.object(_srv.Debug, "save"):
        yield _srv
    _srv.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()



# What Playwright's storage_state() holds: the cookie jar AND each origin's localStorage.
_STATE = {
    "cookies": [],
    "origins": [
        {"origin": "http://127.0.0.1:3000", "localStorage": [{"name": "onboarded", "value": "1"}]}
    ],
}


class _FakePage:
    url = "http://127.0.0.1:3000/slow"

    def __init__(self, goto_fails: bool):
        self._goto_fails = goto_fails

    async def goto(self, url):
        if self._goto_fails:
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")


class _FakeContext:
    def __init__(self, page, built_with: dict | None = None):
        self.pages = [page]
        self.built_with = built_with or {}  # the kwargs _new_context was asked to build it with

    async def storage_state(self):
        return _STATE

    async def close(self):
        pass


def _recording_mgr(goto_fails: bool, new_context_fails: bool = False) -> BrowserManager:
    """Wire a BrowserManager to fake context / page / hooks — parametrised by which step fails."""
    mgr = browser_manager()
    page = _FakePage(goto_fails)
    mgr._context = _FakeContext(page)

    async def ready():
        return None

    async def new_context(*a, **k):
        if new_context_fails:
            raise RuntimeError("browser died")
        mgr._context = _FakeContext(page, built_with=k)

    mgr.ensure_ready = ready
    mgr._new_context = new_context
    mgr.reapply_media = ready
    return mgr


@pytest.mark.asyncio
async def test_a_slow_page_does_not_lose_the_recording():
    mgr = _recording_mgr(goto_fails=True)

    url, trouble = await mgr.start_recording()  # already capturing; a slow reload is not fatal

    assert mgr.is_recording, "the recording was thrown away because the page was slow"
    assert trouble and "could not return to" in trouble, (
        "the caller was told nothing: a warning in the server's log is not the tool result the "
        f"agent reads — got {trouble!r}"
    )


@pytest.mark.asyncio
async def test_a_failed_start_leaves_the_session_able_to_try_again():
    mgr = _recording_mgr(goto_fails=False, new_context_fails=True)

    with pytest.raises(RuntimeError, match="browser died"):
        await mgr.start_recording()

    assert not mgr.is_recording, (
        "a start that never completed still claims to be recording, so every later attempt is "
        "refused with 'Already recording' for the life of the session"
    )


@pytest.mark.asyncio
async def test_it_reports_where_the_page_actually_landed():
    """`record(start=True)` answers with "Current URL: …", and after a failed restore the page is
    NOT there — the difference between a caller that re-navigates and one that acts on a page it
    only thinks it is on.

    An earlier version of this test set the SAME page's url to about:blank, which made
    `start_recording` skip the restore entirely and pass against the unfixed code. The rebuild
    gives the session a FRESH page sitting at about:blank, which is what really happens, so the
    old code would answer with the URL it asked for and the new one answers with where it is.
    """
    mgr = _recording_mgr(goto_fails=True)
    fresh = _FakePage(goto_fails=True)
    fresh.url = "about:blank"

    async def new_context(*a, **k):
        mgr._context = _FakeContext(fresh)

    mgr._new_context = new_context

    assert (await mgr.start_recording())[0] == "about:blank"


async def _start(mgr: BrowserManager):
    await mgr.start_recording()


async def _stop(mgr: BrowserManager):
    await mgr.start_recording()
    await mgr.stop_recording()


async def _viewport_change(mgr: BrowserManager):
    await mgr._rebuild_context()  # what emulate_device does


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", [_start, _stop, _viewport_change])
async def test_a_context_swap_carries_the_whole_storage_state(swap):
    """#123: every path that rebuilds the context re-added the old context's COOKIES and nothing
    else, so localStorage was wiped on record(start) AND again on record(stop) — while
    `document.cookie` survived, which is exactly what made the loss look like the page's own doing.
    Playwright's storage_state() carries both; the rebuilt context must be built from it."""
    mgr = _recording_mgr(goto_fails=False)

    await swap(mgr)

    assert mgr._context.built_with.get("storage_state") == _STATE, (
        "the rebuilt context was not given the session's storage state — localStorage is gone"
    )






def test_a_recording_is_sized_to_the_emulated_device_not_the_desktop_default():
    mgr = browser_manager()
    mgr._device_override = {"width": 390, "height": 664, "is_mobile": True, "has_touch": True}

    kw = mgr._context_kwargs(record_video_dir="/tmp/does-not-matter")

    assert kw["viewport"] == {"width": 390, "height": 664}
    assert kw["record_video_size"] == {"width": 390, "height": 664}, (
        "the frames must be painted at the size the page is rendered at"
    )


def test_a_recording_with_no_emulation_still_uses_the_configured_viewport():
    mgr = browser_manager()
    kw = mgr._context_kwargs(record_video_dir="/tmp/does-not-matter")

    assert kw["record_video_size"] == {
        "width": mgr._config.viewport_width,
        "height": mgr._config.viewport_height,
    }


def test_recording_keeps_the_session_on_its_active_tab():
    """`record` rebuilt the context from `pages[0]`, so after switch_tab it carried the wrong
    page's URL across the rebuild — recording, and then leaving the session on, a different page
    than every other tool was looking at."""

    class FakeContext:
        pages = ["tab0", "tab1", "tab2"]

    mgr = browser_manager()
    mgr._context = FakeContext()

    mgr._active_tab = 2
    assert mgr._active_page() == "tab2"

    mgr._active_tab = 0
    assert mgr._active_page() == "tab0"


def test_active_page_survives_a_tab_closing_under_it():
    """The index can outlive the tab it points at; clamp rather than raise mid-recording."""

    class FakeContext:
        pages = ["only"]

    mgr = browser_manager()
    mgr._context = FakeContext()
    mgr._active_tab = 5
    assert mgr._active_page() == "only"


def test_no_pages_at_all_is_not_a_crash():
    class Empty:
        pages: list = []

    mgr = browser_manager()
    mgr._context = Empty()
    assert mgr._active_page() is None


@pytest.mark.asyncio
async def test_a_rebuilt_context_keeps_the_forced_media_features():
    """emulate_device forces media features onto the context; every path that REBUILDS one has to
    re-assert them. The fix had been applied to the two recording paths and missed _rebuild_context
    — the same sub-bug, one call site over — so it now lives in _new_context, where forgetting it
    is not possible."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.apply_media(reduced_motion="reduce", color_scheme="dark")
        page = await mgr.get_page()
        assert await page.evaluate(
            "() => matchMedia('(prefers-reduced-motion: reduce)').matches"
        ), "setup: the override did not apply at all"

        await mgr._rebuild_context()  # what a viewport / DPR / mobile change does

        page = await mgr.get_page()
        assert await page.evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")
        assert await page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches")
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_recording_keeps_the_pages_local_storage(http_origin):
    """#123: `record(start=True)` swapped the context carrying only its COOKIES, so the page came
    back with its localStorage gone — an app's logged-in / onboarded state — and the agent recorded
    a fresh-origin render, reading its first-visit banner as a "flash" regression. `document.cookie`
    surviving is what pinned the loss on the page instead of the recorder. The stop swap dropped it a
    second time. Real Playwright, real http origin: the mocked test cannot know what storage_state
    holds, only that it was handed over."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.goto(http_origin)
        await page.evaluate("() => localStorage.setItem('onboarded', '1')")

        url, trouble = await mgr.start_recording()

        assert trouble is None and url.rstrip("/") == http_origin
        page = await mgr.get_page()
        assert await page.evaluate("() => localStorage.getItem('onboarded')") == "1", (
            "record(start) wiped the page's localStorage"
        )

        await mgr.stop_recording()

        page = await mgr.get_page()
        assert page.url.rstrip("/") == http_origin
        assert await page.evaluate("() => localStorage.getItem('onboarded')") == "1", (
            "record(stop) wiped the page's localStorage"
        )
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_the_recorded_frames_are_the_emulated_size_on_a_real_recording():
    """#110's symptom was PIXELS — ffprobe said 1280x720 — so the closing evidence has to be the
    produced video, not the kwargs that ask for it."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe not installed")
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.emulate_device(device="iPhone 13")
        await mgr.start_recording()
        page = await mgr.get_page()
        await page.set_content("<body style='background:#0af'>hello</body>")
        await page.wait_for_timeout(400)
        video = await mgr.stop_recording()
        assert video, "no video produced"

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "clip.webm"
            f.write_bytes(video)
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0", str(f)],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        w, h = (int(v) for v in out.split(",")[:2])
        assert (w, h) != (1280, 720), "recorded the desktop default again"
        assert w < 500, f"frames are {w}x{h}, not the emulated mobile viewport"
    finally:
        await mgr.close()






@pytest.mark.asyncio
async def test_browser_recording_keeps_requested_fps_until_stop(monkeypatch):
    from galaius.server import tools_desktop
    from galaius.vision.types import RecordingResult, RecordingCapture, MediaAnalysis

    mgr = browser_manager()
    monkeypatch.setattr(mgr, 'ensure_ready', AsyncMock())
    monkeypatch.setattr(mgr, '_rebuild_context', AsyncMock(return_value=('about:blank', None)))
    captured = []
    async def response(video, **kwargs):
        captured.append(kwargs['fps'])
        return RecordingResult(
            capture=RecordingCapture(status='captured', requested_fps=kwargs['fps'],
                                     timestamp_basis='derived_cadence', observation='indeterminate'),
            analysis=MediaAnalysis(status='not_requested', eligible=None,
                                   attempted=False, input_kind='none'),
        )
    monkeypatch.setattr(tools_desktop, '_record_response', response)
    monkeypatch.setattr(tools_desktop.targets, '_resolve_target', lambda *args: (None, mgr, None))
    started = await tools_desktop.record(start=True, session='fixture', fps=30)
    assert started.structuredContent['capture']['requested_fps'] == 30
    from pathlib import Path
    Path(mgr._recording_dir.name, 'clip.webm').write_bytes(b'fixture video')
    stopped = await tools_desktop.record(start=False, session='fixture')
    assert stopped.structuredContent['capture']['requested_fps'] == 30
    assert captured == [30]
    assert mgr.recording_requested_fps is None


@pytest.mark.asyncio
async def test_record_metadata_reads_encoded_fps_not_requested_sampling(tmp_path):
    import shutil
    import subprocess
    from galaius.server.tools_desktop import _record_metadata

    executable = shutil.which('ffmpeg')
    if executable is None or shutil.which('ffprobe') is None:
        pytest.skip('ffmpeg/ffprobe unavailable')
    clip = tmp_path / 'clip.webm'
    subprocess.run([executable, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc=size=32x32:rate=25:duration=1', '-c:v', 'libvpx', str(clip)],
                   check=True, timeout=10)
    rate, duration = await _record_metadata(clip.read_bytes())
    assert rate == 25
    assert duration == pytest.approx(1)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload, expected', [
    ({'streams': [{'avg_frame_rate': '25/1'}], 'format': {'duration': '2.5'}}, (25, 2.5)),
    ({'streams': [{'avg_frame_rate': '0/0'}], 'format': {}}, (None, None)),
    ({'streams': [{'avg_frame_rate': 'NaN'}], 'format': {'duration': 'inf'}}, (None, None)),
    ({'streams': 'invalid shape'}, (None, None)),
])
async def test_record_metadata_validates_probe_boundary(monkeypatch, payload, expected):
    from galaius.server import tools_desktop

    process = AsyncMock(return_value=(0, json.dumps(payload).encode(), b''))
    monkeypatch.setattr(tools_desktop, 'run_isolated_process', process)
    assert await tools_desktop._record_metadata(b'fixture clip') == expected
    assert process.call_args.kwargs['timeout'] <= 10


@pytest.mark.asyncio
async def test_record_metadata_failure_does_not_invent_an_effective_rate(monkeypatch):
    from galaius.server import tools_desktop

    monkeypatch.setattr(tools_desktop, 'run_isolated_process', AsyncMock(side_effect=TimeoutError))
    assert await tools_desktop._record_metadata(b'fixture clip') == (None, None)


@pytest.mark.asyncio
async def test_failed_browser_recording_drops_requested_fps(monkeypatch):
    mgr = browser_manager()
    monkeypatch.setattr(mgr, 'ensure_ready', AsyncMock())
    monkeypatch.setattr(mgr, '_rebuild_context', AsyncMock(side_effect=RuntimeError('fixture failure')))
    with pytest.raises(RuntimeError, match='fixture failure'):
        await mgr.start_recording(fps=30)
    assert mgr.recording_requested_fps is None
    assert not mgr.is_recording


@pytest.mark.asyncio
@pytest.mark.parametrize('fps', [0, -1])
async def test_record_refuses_invalid_fps_before_target_lookup(monkeypatch, fps):
    from galaius.server import tools_desktop
    from unittest.mock import Mock

    lookup = Mock(side_effect=AssertionError('must reject before target lookup'))
    monkeypatch.setattr(tools_desktop.targets, '_resolve_target', lookup)
    with pytest.raises(ValueError, match='positive'):
        await tools_desktop.record(fps=fps)
    lookup.assert_not_called()






def _rec_win():
    """A mock desktop window with the record-session surface (#61)."""
    from unittest.mock import MagicMock
    from galaius.desktop import DesktopWindow

    win = MagicMock(spec=DesktopWindow)
    win.name = "aino"
    win.w, win.h = 412, 780
    return win


@pytest.mark.asyncio
async def test_record_desktop_start_opens_a_session_not_a_fixed_clip(srv):
    """#61: record(start=True) on a desktop/nested target begins a NON-blocking session and
    returns at once — never the old blocking fixed-duration capture_video (the 3s-clip bug)."""
    win = _rec_win()
    out = await srv._record_desktop(win, query=None, start=True, duration=None, fps=12, path=None)
    win.start_video.assert_called_once_with(12)
    win.capture_video.assert_not_called()           # not the old forced clip
    low = out.analysis.text.lower()
    assert "start=false" in low and "record" in low  # tells the agent how to stop


@pytest.mark.asyncio
async def test_record_desktop_stop_analyzes_the_session_clip(srv, monkeypatch):
    """#61: record(start=False) stops the open session, then analyzes its clip like any video."""
    import galaius.desktop as dt

    win = _rec_win()
    win.stop_video.return_value = b"MP4DATA"
    monkeypatch.setattr(dt.Motion, "is_blank", staticmethod(lambda b: False))
    monkeypatch.setattr(dt.Motion, "detect", staticmethod(lambda b: True))

    async def fake_vlm(media, context, query, role, mime):
        return VLMResult(
            text="a token slides in", elapsed=0.1, model="m",
            dispatch_eligible=True, dispatch_attempted=True, dispatch_status="completed",
        )

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    out = await srv._record_desktop(win, query="what animates?", start=False, duration=None, fps=None, path=None)
    win.stop_video.assert_called_once()
    assert out.analysis.status == "completed" and "slides in" in out.analysis.text


def test_record_sampling_caveat_remains_for_gemini_on_a_session_backend(srv):
    srv.config.media_backend = "session"
    video_model = "gemini/gemini-example-video"
    result = VLMResult(
        text="sequence",
        elapsed=0,
        model=video_model,
        backend="session",
        video_sampled=True,
    )

    caveat = srv.tools_desktop._sampling_caveat(video_model, result)

    assert "sampling floor" in caveat


def test_record_sampling_caveat_reports_the_actual_largest_frame_gap(srv):
    result = VLMResult(
        text="sequence",
        elapsed=0,
        backend="session",
        provider="claude",
        video_sampled=True,
        video_sample_timestamps=[0.0, 1.0, 4.0],
    )

    caveat = srv.tools_desktop._sampling_caveat(result=result)

    assert "~3000ms" in caveat


@pytest.mark.asyncio
async def test_browser_record_caveat_uses_the_actual_native_api_result(srv, monkeypatch):
    mgr = MagicMock()
    mgr.recording_requested_fps = None
    mgr.stop_recording = AsyncMock(return_value=b"WEBM")
    srv.config.media_backend = "session"

    async def native_result(*args, **kwargs):
        return VLMResult(
            text="native sequence",
            elapsed=0,
            model="gemini/example-native-video",
            backend="api",
            provider="gemini",
            video_sampled=False,
            dispatch_eligible=True,
            dispatch_attempted=True,
            dispatch_status="completed",
        )

    monkeypatch.setattr(srv.vlm, "_vlm", native_result)

    out = await srv.tools_desktop._record_browser(
        mgr, start=False, query="what changes?", path=None, session="default"
    )

    assert out.analysis.status == "completed" and "native sequence" in out.analysis.text


@pytest.mark.asyncio
async def test_record_desktop_stop_without_a_session_explains(srv):
    """#61: stopping with no session open says so + names both ways forward, never crashes."""
    win = _rec_win()
    win.stop_video.return_value = None
    out = await srv._record_desktop(win, query=None, start=False, duration=None, fps=None, path=None)
    low = out.analysis.text.lower()
    assert "no recording" in low and "start=true" in low and "duration" in low


@pytest.mark.asyncio
async def test_record_desktop_explicit_duration_stays_a_one_shot_clip(srv, monkeypatch):
    """Backward compat (#62): an explicit duration= is still a blocking one-shot clip — never a
    session — so existing duration-based callers are unaffected."""
    import galaius.desktop as dt

    win = _rec_win()
    win.capture_video.return_value = b"MP4"
    monkeypatch.setattr(dt.Motion, "is_blank", staticmethod(lambda b: False))
    monkeypatch.setattr(dt.Motion, "detect", staticmethod(lambda b: False))
    out = await srv._record_desktop(win, query=None, start=True, duration=2.0, fps=None, path=None)
    win.capture_video.assert_called_once()
    win.start_video.assert_not_called()
    assert out.capture.status == "captured"
    assert out.capture.observation == "indeterminate"



