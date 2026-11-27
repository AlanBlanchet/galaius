"""A capture goes back to the calling model as a picture it reads: sized to stay cheap, skipped (and said so) for a model
with no image input, with no vision model called unless the call asks one (`query`)."""

import base64
import io

import pytest
from PIL import Image as PILImage

import galaius.server as srv
from galaius.account.caller import Caller
from galaius.actions.models import ScreenshotAction
from galaius.agents import registry as run_registry
from galaius.catalog import models as model_catalog
from galaius.server import pictures
from galaius.server.core import mcp
from galaius.server.pictures import CallerModel, Picture, with_pictures
from galaius.vision.types import VLMResult
from tests.support import browser_manager, solid_png


def _decoded(image) -> PILImage.Image:
    return PILImage.open(io.BytesIO(image.data))


@pytest.fixture(autouse=True)
def no_caller_model(monkeypatch):
    """Outside every run unless a test names one: the suite itself may run inside one."""
    monkeypatch.setattr(pictures.CallerModel, "current", classmethod(lambda cls: cls()))


@pytest.fixture
def no_vision_model(monkeypatch):
    async def called(*a, **k):
        pytest.fail("no query: no vision model is called")

    monkeypatch.setattr(srv.vlm, "_vlm", called)
    monkeypatch.setattr(srv.vlm, "_analyze", called)


class _Window:
    wid, name, w, h = 7, "wide", 2560, 720

    def capture(self):
        return solid_png((2560, 720), colour=(200, 30, 30))


async def test_a_file_target_hands_the_picture_back_with_no_query(tmp_path, no_vision_model):
    source = solid_png((640, 360), colour=(0, 128, 255))
    target = tmp_path / "slide.png"
    target.write_bytes(source)
    text, shown = await srv.tools.vision.screenshot(target=f"file:{target}")
    assert shown.data == source and "640x360" in text and "smaller" not in text


async def test_a_wide_window_is_shown_capped_and_saved_whole(tmp_path, monkeypatch, no_vision_model):
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (_Window(), None, None))
    saved = tmp_path / "whole.png"
    text, shown = await srv.tools.vision.screenshot(target="wide", path=str(saved))
    assert _decoded(shown).size == (1280, 360) and PILImage.open(saved).size == (2560, 720)
    assert "capture point (0 + u*2, 0 + v*2)" in text


async def test_the_calls_own_max_width_wins_over_the_default(monkeypatch, no_vision_model):
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (_Window(), None, None))
    text, shown = await srv.tools.vision.screenshot(target="wide", max_width=2000)
    assert _decoded(shown).width == 2000 and "smaller than the capture" not in text


async def test_return_image_false_keeps_the_text_alone(monkeypatch, no_vision_model):
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (_Window(), None, None))
    assert isinstance(await srv.tools.vision.screenshot(target="wide", return_image=False), str)


async def test_a_query_asks_the_vision_model_with_the_whole_capture_and_still_shows_it(monkeypatch):
    seen = {}

    async def fake(data, *args, **kwargs):
        seen["size"] = PILImage.open(io.BytesIO(data)).size
        return VLMResult(text="a red band", elapsed=0.1, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake)
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (_Window(), None, None))
    text, shown = await srv.tools.vision.screenshot(target="wide", query="what colour?")
    assert "a red band" in text and seen["size"] == (2560, 720) and _decoded(shown).width == 1280


async def test_a_text_only_caller_gets_the_text_and_is_told(monkeypatch, no_vision_model):
    monkeypatch.setattr(pictures.CallerModel, "current", classmethod(lambda cls: cls(id="devstral-2512", reads_images=False)))
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (_Window(), None, None))
    out = await srv.tools.vision.screenshot(target="wide")
    assert isinstance(out, str) and "[window: wide]" in out
    assert "devstral-2512 takes no image input" in out and "query=" in out


async def test_a_file_that_is_no_image_is_an_error_line(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello")
    out = await srv.tools.vision.screenshot(target=f"file:{target}")
    assert out.startswith("ERROR") and "not an image" in out


@pytest.mark.parametrize(("size", "fmt", "shown_size", "shown_format"), [
    ((1170, 2532), "PNG", (946, 2048), "png"),     # a phone capture at scale 3: the height cap
    ((2560, 1440), "JPEG", (1280, 720), "jpeg"),   # a photograph stays JPEG once resized
    ((320, 200), "BMP", (320, 200), "png"),        # an encoding no model reads is re-encoded
    ((320, 200), "TIFF", (320, 200), "png"),
])
def test_a_shown_picture_is_sized_and_encoded_for_a_model(size, fmt, shown_size, shown_format):
    out = io.BytesIO()
    PILImage.new("RGB", size, (10, 200, 90)).save(out, format=fmt)
    image, _ = Picture(raw=out.getvalue()).shown(1280)
    assert _decoded(image).size == shown_size and image._format == shown_format


def test_an_oversized_encoding_goes_as_jpeg(monkeypatch):
    monkeypatch.setattr(pictures, "MAX_BYTES", 2000)
    noise = PILImage.effect_noise((200, 200), 90).convert("RGBA")
    out = io.BytesIO()
    noise.save(out, format="PNG")
    image, _ = Picture(raw=out.getvalue()).shown(1280)
    assert image._format == "jpeg" and _decoded(image).size == (200, 200)


async def test_pictures_past_the_limit_are_named_not_sent():
    frames = [Picture(raw=solid_png((8, 8)))] * 14
    text, images = await with_pictures("report", frames)
    assert len(images) == pictures.MAX_PICTURES and "pictures 13 to 14 not attached" in text


# --- who is calling -------------------------------------------------------------------------------


@pytest.mark.parametrize(("model", "reads"), [
    ("claude-opus-5-5", True),            # a reseller row spelled with dots states image input
    ("devstral-2512", False),             # every stated row leaves images out
    ("mistral/devstral-2512", False),
    ("nobody-ever-listed-this", True),    # unknown: handed the picture
])
def test_the_calling_runs_model_decides_from_the_cached_catalog(monkeypatch, model, reads):
    monkeypatch.undo()  # the real `CallerModel.current`
    rows = [{"id": "anthropic/claude-opus-5.5", "input_modalities": ["text", "image"]},
            {"id": "mistralai/devstral-2512", "input_modalities": ["text", "file"]}]
    monkeypatch.setattr(type(model_catalog.CACHE), "read", lambda self: {"source": "openrouter", "models": rows})
    monkeypatch.setattr(run_registry, "get_run", lambda run_id: type("Run", (), {"model": model})() if run_id == "r1" else None)
    with Caller(environment={"GALAIUS_RUN_ID": "r1"}, cwd="/", pid=0).bound():
        assert CallerModel.current().reads_images is reads


def test_a_litellm_catalog_never_calls_a_model_text_only(monkeypatch):
    rows = [{"id": "mistral/devstral-2512", "input_modalities": ["text"]}]
    monkeypatch.setattr(type(model_catalog.CACHE), "read", lambda self: {"source": "litellm", "models": rows})
    assert model_catalog.reads_images("devstral-2512") is None


@pytest.mark.parametrize(("capabilities", "reads"), [(("completion",), False), (("completion", "vision"), True)])
def test_a_model_the_local_daemon_serves_is_answered_by_the_daemon(monkeypatch, capabilities, reads):
    from galaius.inference.ollama import OllamaModel

    monkeypatch.setattr(pictures.ollama, "discover_cached", lambda: [OllamaModel(name="qwen3:0.6b", capabilities=capabilities)])
    assert CallerModel._image_input("qwen3:0.6b") is reads


def test_a_client_outside_every_run_is_handed_the_picture(monkeypatch):
    monkeypatch.undo()
    with Caller(environment={}, cwd="/", pid=0).bound():
        assert CallerModel.current() == CallerModel()


# --- run_actions ----------------------------------------------------------------------------------


async def test_run_actions_screenshot_steps_come_back_as_pictures(monkeypatch):
    mgr = browser_manager()
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")
    try:
        page = await mgr.get_page(0)
        await page.set_content("<title>T</title><body style='margin:0;background:#c00'><button>go</button></body>")
        monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (None, mgr, None))
        result = await mcp.call_tool("run_actions", {"actions": [
            {"type": "screenshot"}, {"type": "screenshot", "return_image": False},
            {"type": "screenshot", "selector": "button"}, {"type": "screenshot", "max_width": 200},
        ]})
        images = [block for block in result.content if block.type == "image"]
        text = result.structuredContent["result"]
        assert len(images) == 3 and "[picture 3]" in text and "[picture 4]" not in text, text
        assert PILImage.open(io.BytesIO(base64.b64decode(images[2].data))).width == 200

        element_text, element = await srv.tools.vision.screenshot(selector="button")
        assert "button" in element_text and _decoded(element).width < 200
    finally:
        await mgr.close()
