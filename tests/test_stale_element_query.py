"""#112 (second half): the reporter asked that the analysis path not be able to see text from
outside the frame it is describing — "anything that lets step text leak into 'what is on screen'
makes captions unfalsifiable".

There is a real instance of exactly that. `screenshot(target=<window>, element=N, query=...)`
crops the CURRENT frame at coordinates from a PREVIOUS detection and captions it with that older
widget's role and name, with no freshness check — while the sibling no-query branch right below it
already gates its refs on the frame's content signature (#19). So the model is handed a picture of
one thing labelled as another, which is the ideal setup for the confident wrong answer #112 is about.
"""

import pytest

from interact.desktop import DesktopElement
from tests.support import varied_png


def test_refs_detected_on_another_frame_are_reported_stale():
    wid = 4242
    DesktopElement.merge_into(wid, [DesktopElement(index=0, x=1, y=2, w=3, h=4, role="button", name="Run")], "sigA")

    assert DesktopElement.stale_for(wid, "sigB") is True, "screen changed → refs describe a gone frame"
    assert DesktopElement.stale_for(wid, "sigA") is False, "same frame → refs still describe it"


def test_a_window_never_detected_is_not_stale():
    """Nothing detected yet is a different situation from a detection that went out of date."""
    assert DesktopElement.stale_for(987654, "sig") is False


@pytest.mark.asyncio
async def test_a_stale_ref_loses_its_LABEL_but_still_gets_looked_at(monkeypatch):
    """Not a refusal. The signature is a 16x16 hash of the frame, so a blinking caret or a clock
    flips it — refusing there would make element queries unusable on any live window, which is a
    worse failure than the one being fixed. What must not survive a screen change is the LABEL:
    the model must never be told that these pixels are a widget detected on another frame."""
    import interact.server as srv

    wid = 5150

    class FakeWin:
        def __init__(self):
            self.wid, self.name, self.w, self.h = wid, "Code", 800, 600

        def capture(self):  # a frame that does NOT match the seeded detection
            return varied_png()

    DesktopElement.merge_into(
        wid, [DesktopElement(index=0, x=0, y=0, w=10, h=10, role="button", name="Old Button")], "sigOLD"
    )
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (FakeWin(), None, None))

    seen: dict = {}

    async def capture_context(data, context, query=None, *a, **k):
        seen["context"] = context
        return srv.vlm._MediaResponse("a region of colour", None)

    monkeypatch.setattr(srv.vlm, "_media_response", capture_context)

    out = await srv.tools_vision.screenshot(target="Code", element=1, query="what is this?")

    assert "Old Button" not in seen["context"], "the gone widget's name reached the model"
    assert "10x10 at 0,0" in seen["context"], "it should still say WHERE the crop came from"
    assert "different frame" in out and "get_interactive_elements" in out, out


@pytest.mark.asyncio
async def test_a_fresh_ref_keeps_its_label(monkeypatch):
    import interact.server as srv
    from interact.vision.detect import _page_signature

    wid = 5151
    frame = varied_png()

    class FakeWin:
        def __init__(self):
            self.wid, self.name, self.w, self.h = wid, "Code", 800, 600

        def capture(self):
            return frame

    DesktopElement.merge_into(
        wid, [DesktopElement(index=0, x=0, y=0, w=10, h=10, role="button", name="Run")],
        _page_signature(frame),
    )
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (FakeWin(), None, None))

    seen: dict = {}

    async def capture_context(data, context, query=None, *a, **k):
        seen["context"] = context
        return srv.vlm._MediaResponse("a button", None)

    monkeypatch.setattr(srv.vlm, "_media_response", capture_context)

    out = await srv.tools_vision.screenshot(target="Code", element=1, query="what is this?")
    assert "Run" in seen["context"]
    assert "different frame" not in out
