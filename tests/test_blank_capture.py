"""#112: a `screenshot(query=...)` on a desktop window answered with the agent's own action text
("press ctrl+shift+p") wrapped in a full-frame bounding box, as if it had read it on screen.

The window had in fact CRASHED and the capture came back all black. Handed an empty image, the
model reconstructed something plausible from the prompt instead of reporting that there was
nothing to see — so the captioned path reported nothing-unusual while the app was dead. That is
the failure mode the reporter rightly called worse than an error: a well-formed, plausible,
completely wrong answer that reads as a real observation.

A blank frame is deterministically detectable, so it never needs a model's opinion. Detect it,
say so, and don't spend a VLM call inventing an answer about an empty image.
"""

import subprocess

import pytest

from interact.desktop import DesktopWindow
from interact.vision.measure import blank_frame_reason


from tests.support import solid_png, varied_png as _varied_png

def test_an_all_black_capture_is_reported_blank_with_its_colour():
    reason = blank_frame_reason(solid_png((320, 200), colour=(0, 0, 0)))
    assert reason and "#000000" in reason, reason


def test_a_white_capture_is_blank_too():
    """A crashed GPU surface grabs black; a blank page grabs white. Both are "nothing to see"."""
    assert blank_frame_reason(solid_png((320, 200), colour=(255, 255, 255)))


def test_the_check_errs_toward_sending_the_frame():
    """The two mistakes are not symmetric. Wrongly calling a real screen blank REFUSES to look at
    it — the failure this whole gate exists to avoid, in the other direction. Wrongly calling an
    empty one real just spends a model call. So visible content at small size wins, even a
    scattering of it."""
    assert blank_frame_reason(solid_png((320, 200), colour=(0, 0, 0), speckle=40)) is None


def test_a_full_size_crashed_window_is_caught():
    """The reported case, at the size it was reported at: an entirely black 1440x900 capture of a
    window that had crashed."""
    assert blank_frame_reason(solid_png((1440, 900), colour=(0, 0, 0)))


def test_a_real_screenful_is_not_blank():
    assert blank_frame_reason(_varied_png()) is None


@pytest.mark.asyncio
async def test_a_blank_capture_is_never_sent_to_the_vlm():
    """No mock needed to prove the model was not called: the conftest fixture fails any real
    litellm call in a non-integration test, so reaching one would blow up rather than pass."""
    import interact.server as srv

    out = await srv.vlm._media_response(
        solid_png((400, 300), colour=(0, 0, 0)), "Desktop window: Code (1920x1080)", "what is on screen?"
    )
    assert out.text and out.text.startswith("ERROR:"), out
    assert "blank" in out.text.lower()


@pytest.mark.asyncio
async def test_a_real_frame_still_reaches_the_vlm(monkeypatch):
    import interact.server as srv

    from interact.vision import VLMResult

    async def ok(*a, **k):
        return VLMResult(text="a toolbar and a sidebar", elapsed=0.1, model="test")

    monkeypatch.setattr(srv.vlm, "_vlm", ok)
    out = await srv.vlm._media_response(_varied_png(), "ctx", "what is on screen?")
    assert out.text and "toolbar" in out.text


# --- #113: the same emptiness has to be surfaced on the UNCAPTIONED path too ---
#
# A per-window capture of a CRASHED VS Code window returned an entirely black 1440x900 PNG with no
# comment. Black is indistinguishable from "still loading", so the reporter waited and retried —
# two capture rounds and ~30s — while `target="screen"` would have shown VS Code's own "window
# terminated unexpectedly" modal immediately. They asked for exactly one line in the tool result.


@pytest.mark.asyncio
async def test_a_black_window_capture_says_so_even_with_no_query(monkeypatch):
    import interact.server as srv

    class DeadWindow:
        wid, name, w, h = 33554476, "interact - Visual Studio Code", 1440, 900

        def capture(self):
            return solid_png((240, 150), colour=(0, 0, 0))

    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (DeadWindow(), None, None))

    out = await srv.tools_vision.screenshot(target="interact - Visual Studio Code")
    text = out[0] if isinstance(out, list) else out

    assert "#000000" in text, text
    assert 'target="screen"' in text, "the fallback that reveals the crash must be named"


@pytest.mark.asyncio
async def test_a_normal_window_capture_carries_no_such_note(monkeypatch):
    import interact.server as srv

    class LiveWindow:
        wid, name, w, h = 1, "Code", 240, 150

        def capture(self):
            return _varied_png(size=(240, 150))

    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (LiveWindow(), None, None))

    out = await srv.tools_vision.screenshot(target="Code")
    text = out[0] if isinstance(out, list) else out
    assert "uniform" not in text.lower(), text


# --- the header fast path must not create a blind spot at small sizes ---
#
# Screening on encoded bytes-per-pixel is only valid while the PNG's fixed header is negligible
# against the pixel count. It is not, on a small crop: an 8x8 blank frame encodes at 1.08 b/px,
# twenty times the threshold, so the very check that makes this affordable on a 4K grab was
# skipping every small one. `screenshot(element=N, query=...)` crops single widgets — an icon, a
# button — which is exactly the size where a blank frame stopped being detected.


@pytest.mark.parametrize("size", [(8, 8), (16, 16), (24, 24), (32, 32), (48, 48), (120, 90)])
def test_a_blank_crop_is_detected_at_any_size(size):
    assert blank_frame_reason(solid_png(size, colour=(0, 0, 0))), f"{size[0]}x{size[1]} blank frame missed"


@pytest.mark.parametrize("size", [(16, 16), (64, 64), (200, 200)])
def test_a_small_busy_crop_is_still_not_blank(size):
    assert blank_frame_reason(_varied_png(size=size)) is None


@pytest.mark.asyncio
async def test_the_judgement_tools_are_gated_too():
    """review_ui and verify_ui reach the model through `_vlm` directly rather than through
    `_media_response`, and describing a frame is their entire job — so gating only the screenshot
    path left the two tools most exposed to #112 still exposed."""
    import interact.server as srv

    r = await srv.vlm._vlm(solid_png((400, 300), colour=(0, 0, 0)), "ctx", "what is wrong here?")
    assert r.text.startswith("ERROR:") and "blank" in r.text
    assert r.model == "(not called)", "it must be visible that no model ran"


# --- A blank capture has TWO causes, and naming only one sends people the wrong way -----------
#
# Issue #113: a VS Code window had CRASHED ("The window terminated unexpectedly"), and per-window
# capture returned uniform black. The error named a GPU surface and prescribed picom / adb — a
# confident diagnosis of the wrong problem. The reporter's own fix was `target="screen"`, which
# the message never mentioned, and they burned two capture rounds and ~30s waiting for a window
# that was never coming back.


def _blank_capture(monkeypatch, *, pid: str | None, alive: bool):
    """A window whose every capture path comes back uniform, with a chosen liveness answer."""
    black = solid_png(40, 40, (0, 0, 0))

    def fake(cmd, *a, **k):
        if cmd[0] == "xdotool":
            if "getwindowpid" in cmd:
                if pid is None:
                    raise subprocess.CalledProcessError(1, cmd)
                return f"{pid}\n"
            return "WIDTH=388\nHEIGHT=863\nX=0\nY=0\n"
        return black

    monkeypatch.setattr("interact.desktop.subprocess.check_output", fake)
    monkeypatch.setattr("interact.desktop.window._pid_alive", lambda _pid: alive)


def test_a_blank_capture_of_a_DEAD_window_says_the_window_is_gone(monkeypatch):
    """The process behind it no longer exists, so no compositor setting will ever help."""
    from interact.desktop import CaptureError

    _blank_capture(monkeypatch, pid="4242", alive=False)
    win = DesktopWindow(name="interact - Visual Studio Code", wid=123, x=0, y=0, w=388, h=863)
    with pytest.raises(CaptureError) as exc:
        win.capture()
    msg = str(exc.value)
    assert "no longer running" in msg or "not running" in msg, msg
    assert "GPU" not in msg, "a dead window is not a GPU-surface problem — do not say it is"
    assert "4242" in msg, "name the pid, so the claim can be checked"


def test_a_blank_capture_of_a_LIVE_window_still_reports_the_gpu_cause(monkeypatch):
    """The original diagnosis stays for the case it was right about."""
    from interact.desktop import CaptureError

    _blank_capture(monkeypatch, pid="4242", alive=True)
    win = DesktopWindow(name="Android Emulator - Pixel_7:5554", wid=123, x=0, y=0, w=388, h=863)
    with pytest.raises(CaptureError) as exc:
        win.capture()
    assert "GPU" in str(exc.value)


def test_every_blank_capture_offers_the_screen_fallback(monkeypatch):
    """The cheap workaround the reporter found unaided. A whole-screen grab reveals crashes,
    modals and anything else a per-window grab cannot read — and costs one call."""
    from interact.desktop import CaptureError

    for alive in (True, False):
        _blank_capture(monkeypatch, pid="4242", alive=alive)
        win = DesktopWindow(name="whatever", wid=123, x=0, y=0, w=388, h=863)
        with pytest.raises(CaptureError) as exc:
            win.capture()
        assert 'target="screen"' in str(exc.value), f"alive={alive}: no fallback offered"


def test_an_unknowable_pid_does_not_become_a_liveness_claim(monkeypatch):
    """xdotool cannot always answer. Silence is not evidence the window is alive OR dead, and
    asserting either from a failed lookup is how a wrong diagnosis gets stated confidently."""
    from interact.desktop import CaptureError

    _blank_capture(monkeypatch, pid=None, alive=True)
    win = DesktopWindow(name="whatever", wid=123, x=0, y=0, w=388, h=863)
    with pytest.raises(CaptureError) as exc:
        win.capture()
    msg = str(exc.value)
    assert "no longer running" not in msg
    assert 'target="screen"' in msg


def test_a_window_whose_grab_FAILS_outright_is_reported_not_raised_raw(monkeypatch):
    """Found by killing a real window rather than mocking one: for a genuinely dead window `maim
    -i <wid>` does not return black at all — it exits non-zero. So the uniform-colour path never
    runs, and what actually reached the agent was a raw CalledProcessError traceback naming a
    numeric window id. Every real dead-window case took this branch, not the one above it.
    """
    from interact.desktop import CaptureError

    def fake(cmd, *a, **k):
        if cmd[0] == "xdotool":
            if "getwindowpid" in cmd:
                return "4242\n"
            raise subprocess.CalledProcessError(1, cmd)
        raise subprocess.CalledProcessError(1, cmd)  # maim cannot read a dead window

    monkeypatch.setattr("interact.desktop.subprocess.check_output", fake)
    monkeypatch.setattr("interact.desktop.window._pid_alive", lambda _pid: False)
    win = DesktopWindow(name="doomed", wid=123, x=0, y=0, w=300, h=200)
    with pytest.raises(CaptureError) as exc:
        win.capture()
    msg = str(exc.value)
    assert "doomed" in msg, "name the window, not just a numeric id"
    assert 'target="screen"' in msg


def test_the_local_backend_reports_an_unreadable_window_the_same_way(monkeypatch):
    """The same defect one layer down: LocalBackend.capture_window runs maim with check=True, so
    a dead window raised a bare CalledProcessError there too. One report is one sample of a class;
    both capture paths have to answer the same way or the message you get depends on which
    internal route your target happened to take.
    """
    from interact.desktop import CaptureError
    from interact.desktop.backend import LocalBackend

    def fake_run(cmd, *a, **k):
        if cmd[0] == "xdotool":
            return subprocess.CompletedProcess(cmd, 0, stdout="555\n", stderr="")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("interact.desktop.backend.subprocess.run", fake_run)
    backend = LocalBackend.__new__(LocalBackend)  # no real uinput device in a unit test
    with pytest.raises(CaptureError) as exc:
        backend.capture_window("doomed")
    assert "doomed" in str(exc.value)


def test_blank_gpu_surface_capture_raises_actionable_error(monkeypatch):
    """An Android-emulator / GPU-surface window grabs uniform black via X — don't hand back a
    black image; raise a clear error naming the cause + the adb/compositor fixes."""
    import io
    from PIL import Image as PILImage
    from interact.desktop import CaptureError

    buf = io.BytesIO()
    PILImage.new("RGB", (40, 40), "black").save(buf, format="PNG")
    black = buf.getvalue()

    def fake(cmd, *a, **k):  # maim → black; xdotool geometry → a valid region
        if cmd[0] == "xdotool":
            return "WIDTH=388\nHEIGHT=863\nX=0\nY=0\n"
        return black

    monkeypatch.setattr("interact.desktop.subprocess.check_output", fake)
    win = DesktopWindow(name="Android Emulator - Pixel_7:5554", wid=123, x=0, y=0, w=388, h=863)
    with pytest.raises(CaptureError) as exc:
        win.capture()
    msg = str(exc.value)
    assert "GPU" in msg and "adb" in msg and "Android Emulator" in msg
