"""`galaius.vision.detect._vlm_detect_elements` — how a screenshot becomes UI elements.

The component→image model fallback chain (with a circuit breaker to skip a known-broken
component model), model_override bypassing that chain, the session-backend path, structured
(Pydantic) vs text-fallback parsing, the format-specific detection prompt, coordinate rescale
for oversized images, and the desktop-side filtering (AtSpi shadow-crop, window-manager-only
button rejection) that runs before detection on a desktop target.
"""

import io
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image as PILImage

import galaius.vision.detect as det
from galaius.models import Model, ModelCapability
from galaius.vision import MediaItem, VLMResult
from tests.support.models import catalog_of

_DESKTOP_CTX = "Desktop window: Test (800x600)"

# A frame with CONTENT in it, not a flat fill: an empty frame is now short-circuited before any
# model call (#112), so a 1x1 red square would make every fallback test below assert on a chain
# that never ran.
_img = PILImage.new("RGB", (8, 8))
for _x in range(8):
    for _y in range(8):
        _img.putpixel((_x, _y), (_x * 32, _y * 32, 0))
_buf = io.BytesIO()
_img.save(_buf, format="PNG")
_PNG = _buf.getvalue()
_VLM_JSON = '[{"role":"button","name":"Save","x":100,"y":200,"w":150,"h":30}]'


@pytest.fixture
def srv(provider_key):
    import galaius.server as _srv
    from galaius.server import breaker

    breaker.clear()
    _srv.config.component_criteria = "cap.gui_grounding"
    with patch.object(_srv.Debug, "save"):
        yield _srv
    _srv.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()


@pytest.mark.asyncio
async def test_vlm_detect_elements_component_failure_returns_none(srv):
    """When component model is available but fails, no fallback to image — returns None."""
    fail = AsyncMock(side_effect=RuntimeError("missing API key"))
    with patch.object(srv.vlm, "_vlm", fail):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, _DESKTOP_CTX, 800, 600
        )

    assert fail.call_count == 1
    assert fail.call_args_list[0].kwargs.get("media_type") == "component"
    assert elements is None


@pytest.mark.asyncio
async def test_vlm_detect_elements_uses_only_component_model(srv):
    succeed = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.5))
    with patch.object(srv.vlm, "_vlm", succeed):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, _DESKTOP_CTX, 800, 600
        )

    assert succeed.call_count == 1
    assert succeed.call_args_list[0].kwargs.get("media_type") == "component"
    assert elements is not None


@pytest.mark.parametrize(
    "soft_fail_msg",
    [
        "[Vision unavailable — model API key not configured]",
        "[Vision unavailable — component key missing]",
    ],
)
@pytest.mark.asyncio
async def test_vlm_detect_elements_soft_failure_no_fallback(srv, soft_fail_msg):
    """Component soft-fails → no fallback to image, returns None."""
    soft_fail = VLMResult(text=soft_fail_msg, elapsed=0.1)
    mock_vlm = AsyncMock(return_value=soft_fail)
    with patch.object(srv.vlm, "_vlm", mock_vlm):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, _DESKTOP_CTX, 800, 600
        )

    assert mock_vlm.call_count == 1
    assert mock_vlm.call_args_list[0].kwargs.get("media_type") == "component"
    assert elements is None
    assert raw == ""


def test_element_detection_prompt_generic():
    from galaius.formats import CoordFormat

    prompt = CoordFormat().prompt(1920, 1080)
    assert "pixel coordinates" in prompt
    assert '{"role":"button"' in prompt
    assert "box_2d" not in prompt
    assert "1920" in prompt
    assert "1080" in prompt


@pytest.mark.asyncio
async def test_vlm_detect_elements_fallback_uses_generic_prompt(srv):
    """When component model fails, it uses format-specific prompt."""
    from galaius.formats import CoordFormat

    fail = AsyncMock(side_effect=RuntimeError("missing API key"))
    CoordFormat.load_from_config(
        {
            "gemini/": {
                "normalized": True,
                "box_order": "yxyx",
                "box_key": "box_2d",
                "prompt_template": (
                    "Return as JSON array. For each element provide bounding box coordinates "
                    "in [ymin, xmin, ymax, xmax] format where values range from 0 to 1000. "
                    '[{{"role":"button","name":"OK","box_2d":[200,100,260,180]}}]'
                ),
            },
            "zai/": {"normalized": True, "box_order": "xyxy"},
        }
    )
    with (
        patch.object(srv.vlm, "_vlm", fail),
        patch.object(srv.core, "config") as mock_config,
    ):
        mock_config.resolve_model.side_effect = lambda role, *a, **k: (
            "gemini/gemini-2.0-flash" if role == "component" else "openai/gpt-4.1"
        )
        mock_config.vlm_max_dim = 1280
        mock_config.vlm_min_dim = 768
        await det._vlm_detect_elements(_PNG, _DESKTOP_CTX, 800, 600)

    # Gemini component model gets box_2d format prompt
    first_prompt = fail.call_args_list[0].args[2]
    assert "pixel coordinates" in first_prompt
    assert "box_2d" not in first_prompt
    CoordFormat.load_from_config({})


@pytest.fixture
def one_component_candidate(monkeypatch):
    """A scoped catalog with exactly ONE model clearing the component role's criterion, so
    tripping it leaves nothing else to walk to within "component" — the deterministic case a
    circuit-breaker test needs, independent of how many bundled models the real catalog ships."""
    monkeypatch.setattr(Model, "is_available", lambda self: True)
    with catalog_of(
        # GUI_GROUNDING only — never VLM — so it clears "component"'s criterion but never
        # "image"'s, keeping the two roles' resolved ids genuinely distinct.
        Model(id="test/comp-only", provider="test", capabilities={ModelCapability.GUI_GROUNDING}),
        Model(id="test/image-only", provider="test", capabilities={ModelCapability.VLM}),
    ):
        yield


@pytest.mark.asyncio
async def test_circuit_breaker_skips_after_failure(srv, one_component_candidate):
    # First call: component fails, trips breaker
    fail = AsyncMock(side_effect=RuntimeError("key error"))
    with patch.object(srv.vlm, "_vlm", fail):
        await det._vlm_detect_elements(_PNG, "ctx", 800, 600)
    assert fail.call_count == 1

    # Second call: the only component candidate is tripped, nothing left to walk within
    # "component" → falls to image (1 call)
    image_only = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.3))
    with patch.object(srv.vlm, "_vlm", image_only):
        await det._vlm_detect_elements(_PNG, "ctx", 800, 600)
    assert image_only.call_count == 1
    assert image_only.call_args.kwargs.get("media_type") == "image"


@pytest.mark.asyncio
async def test_circuit_breaker_resets_after_ttl(srv, one_component_candidate):
    from galaius.server import breaker

    component_model = srv.config.resolve_model("component")
    # Trip the breaker with a timestamp in the past (beyond TTL)
    breaker.trip(component_model)
    breaker._trips[component_model] = time.monotonic() - breaker._ttl - 1

    mock_vlm = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.5))
    with patch.object(srv.vlm, "_vlm", mock_vlm):
        await det._vlm_detect_elements(_PNG, "ctx", 800, 600)

    # Should have tried component model (breaker reset) — only component runs
    assert mock_vlm.call_count == 1
    assert mock_vlm.call_args_list[0].kwargs.get("media_type") == "component"


@pytest.mark.asyncio
async def test_low_element_count_warning(srv, caplog):
    few_elements = '[{"role":"button","name":"OK","x":100,"y":200,"w":80,"h":30}]'
    mock_vlm = AsyncMock(return_value=VLMResult(text=few_elements, elapsed=0.5))
    with patch.object(srv.vlm, "_vlm", mock_vlm):
        import logging

        with caplog.at_level(logging.WARNING, logger="galaius"):
            await det._vlm_detect_elements(_PNG, "ctx", 800, 600)

    assert any("Low element count" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "elements_data, expected",
    [
        (
            [{"name": "OK", "role": "button", "x": 100, "y": 200, "w": 80, "h": 30}],
            [(1, 100, 200, 80, 30, "button", "OK")],
        ),
        (
            [
                {"name": "A", "role": "link", "x": 10, "y": 20, "w": 40, "h": 15},
                {"name": "B", "role": "input", "x": 60, "y": 80, "w": 100, "h": 25},
            ],
            [
                (1, 10, 20, 40, 15, "link", "A"),
                (2, 60, 80, 100, 25, "input", "B"),
            ],
        ),
        ([], []),
    ],
    ids=["single", "multi-element", "empty"],
)
def test_structured_to_elements(srv, elements_data, expected):
    detection = det._DetectionResult(
        elements=[det._DetectedElement(**d) for d in elements_data]
    )
    result = det._structured_to_elements(detection)
    assert len(result) == len(expected)
    for el, (idx, x, y, w, h, role, name) in zip(result, expected):
        assert (el.index, el.x, el.y, el.w, el.h, el.role, el.name) == (
            idx,
            x,
            y,
            w,
            h,
            role,
            name,
        )


def test_structured_to_elements_raw_coords(srv):
    """_structured_to_elements returns raw coords; caller applies CoordTransform for clamping."""
    detection = det._DetectionResult(
        elements=[
            det._DetectedElement(name="OK", role="button", x=800, y=880, w=150, h=40),
            det._DetectedElement(name="In", role="input", x=100, y=200, w=80, h=30),
        ]
    )
    result = det._structured_to_elements(detection)
    # Raw coords preserved — no clamping in parse step
    assert result[0].x == 800
    assert result[0].y == 880
    assert result[0].w == 150
    assert result[0].h == 40
    assert result[1].x == 100
    assert result[1].y == 200


@pytest.mark.parametrize(
    "vlm_text, expect_elements",
    [
        ('[{"role":"button","name":"OK","x":10,"y":20,"w":80,"h":30}]', True),
        ("NOT VALID JSON {{{", False),
    ],
    ids=["valid-fallback-json", "truly-invalid"],
)
@pytest.mark.asyncio
async def test_structured_fallback_on_invalid_json(
    srv, caplog, vlm_text, expect_elements
):
    """When structured parse fails, _vlm_detect_elements falls back to text parsing."""
    import logging

    mock_vlm = AsyncMock(return_value=VLMResult(text=vlm_text, elapsed=0.5))
    with (
        patch.object(srv.vlm, "_vlm", mock_vlm),
        patch.object(det, "_model_supports_structured", return_value=True),
    ):
        with caplog.at_level(logging.WARNING, logger="galaius"):
            elements, elapsed, raw, _ = await det._vlm_detect_elements(
                _PNG, "ctx", 800, 600
            )

    assert any("Structured parse failed" in r.message for r in caplog.records)
    if expect_elements:
        assert elements is not None
    else:
        assert elements is None


@pytest.mark.parametrize(
    "elements_json, expected_names",
    [
        (
            '{"elements":[{"name":"OK","role":"button","x":100,"y":200,"w":80,"h":30}]}',
            ["OK"],
        ),
        (
            '{"elements":[{"name":"Save","role":"button","x":10,"y":20,"w":60,"h":25},{"name":"URL","role":"input","x":200,"y":50,"w":300,"h":30},{"name":"Help","role":"link","x":400,"y":100,"w":40,"h":15}]}',
            ["Save", "URL", "Help"],
        ),
        (
            '{"elements":[]}',
            [],
        ),
    ],
    ids=["single", "multiple", "empty"],
)
@pytest.mark.asyncio
async def test_structured_output_happy_path(srv, elements_json, expected_names):
    """Structured output parsed via Pydantic, not text fallback."""
    mock_vlm = AsyncMock(return_value=VLMResult(text=elements_json, elapsed=0.5))
    with (
        patch.object(srv.vlm, "_vlm", mock_vlm),
        patch.object(det, "_model_supports_structured", return_value=True),
    ):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, "ctx", 800, 600
        )

    for call in mock_vlm.call_args_list:
        assert call.kwargs.get("response_format") is det._DetectionResult
    if expected_names:
        assert elements is not None
        assert [el.name for el in elements] == expected_names
    else:
        assert elements is None


@pytest.mark.asyncio
async def test_enqueue_no_structured_passes_none_format(srv):
    """_enqueue passes response_format=None when model does not support structured output."""
    mock_vlm = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.5))
    with (
        patch.object(srv.vlm, "_vlm", mock_vlm),
        patch.object(det, "_model_supports_structured", return_value=False),
    ):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, "ctx", 800, 600
        )

    for call in mock_vlm.call_args_list:
        assert call.kwargs.get("response_format") is None

    assert elements is not None


def _desktop_el(name, role="push button", y=10):
    from galaius.desktop import DesktopElement

    return DesktopElement(index=1, role=role, name=name, x=0, y=y, w=30, h=20)


@pytest.mark.parametrize(
    "elements, expected",
    [
        # All WM buttons in title bar → True
        (
            [_desktop_el("Close"), _desktop_el("Minimize"), _desktop_el("Maximize")],
            True,
        ),
        # Single WM button → True
        ([_desktop_el("Restore")], True),
        # Mixed: WM + non-WM → False
        ([_desktop_el("Close"), _desktop_el("Save")], False),
        # Dialog Close at y=200 (below title bar) → False
        ([_desktop_el("Close", y=200)], False),
        # Right role, right name, wrong role → False
        ([_desktop_el("Close", role="button")], False),
        # Empty list → False
        ([], False),
        # British spelling variants
        ([_desktop_el("Minimise"), _desktop_el("Maximise")], True),
    ],
    ids=[
        "wm-only",
        "single-wm",
        "mixed",
        "dialog-close-low-y",
        "wrong-role",
        "empty",
        "british-spelling",
    ],
)
def test_is_wm_only(srv, elements, expected):
    assert det._is_wm_only(elements) is expected


@pytest.mark.asyncio
async def test_vlm_detect_elements_rescales_for_large_images(srv):
    """VLM coordinates are rescaled back to original image dimensions."""
    from PIL import Image as PILImage
    import io

    # Create a real 1920x1080 PNG
    img = PILImage.new("RGB", (1920, 1080), color="green")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    # VLM returns coords in 1280x720 space
    vlm_json = '[{"role":"button","name":"OK","x":640,"y":360,"w":100,"h":50}]'
    mock_vlm = AsyncMock(return_value=VLMResult(text=vlm_json, elapsed=0.5))
    with patch.object(srv.vlm, "_vlm", mock_vlm):
        elements, _, _, _ = await det._vlm_detect_elements(png, "ctx", 1920, 1080)

    assert elements is not None
    el = elements[0]
    # 640 * 1.5 = 960, 360 * 1.5 = 540, etc.
    assert el.x == 960
    assert el.y == 540
    assert el.w == 150
    assert el.h == 75


@pytest.mark.asyncio
async def test_vlm_detect_elements_model_override_bypasses_component(srv):
    """model_override skips component model and uses the override directly."""
    succeed = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.3))
    with patch.object(srv.vlm, "_vlm", succeed):
        elements, elapsed, raw, label = await det._vlm_detect_elements(
            _PNG,
            _DESKTOP_CTX,
            800,
            600,
            model_override="custom/override-model",
        )

    assert succeed.call_count == 1
    # media_type should be "override" not "component"
    assert succeed.call_args_list[0].kwargs.get("media_type") == "override"
    # model_override passed through to _vlm
    assert (
        succeed.call_args_list[0].kwargs.get("model_override")
        == "custom/override-model"
    )
    assert elements is not None
    assert label == "custom/override-model"


@pytest.mark.asyncio
async def test_session_detection_uses_provider_neutral_schema_and_actual_result_identity(
    srv, monkeypatch
) -> None:
    srv.config.media_backend = "session"
    srv.config.media_billing = "session_only"
    captured: dict = {}
    debug: dict = {}

    def forbidden_api_capability_probe(model: str) -> bool:
        raise AssertionError("session detection consulted API model capabilities")

    async def session_vlm(data, context, prompt, **kwargs):
        captured.update(kwargs)
        payload = det._DetectionResult(elements=[
            det._DetectedElement(name="Save", role="button", x=10, y=20, w=30, h=12)
        ])
        return VLMResult(
            text=payload.model_dump_json(),
            elapsed=0.2,
            model="claude-session-model",
            backend="session",
            provider="claude",
        )

    def save_debug(name, value, **kwargs):
        if name == "vlm_meta":
            debug.update(json.loads(value))

    monkeypatch.setattr(det, "_model_supports_structured", forbidden_api_capability_probe)
    monkeypatch.setattr(srv.vlm, "_vlm", session_vlm)
    monkeypatch.setattr(det.Debug, "save", save_debug)

    elements, _, _, label = await det._vlm_detect_elements(
        _PNG, _DESKTOP_CTX, 800, 600
    )

    assert elements and elements[0].name == "Save"
    assert captured["response_format"] is det._DetectionResult
    assert captured["model_override"] is None
    assert label == "claude-session-model"
    assert debug["model"] == "claude-session-model"
    assert debug["provider"] == "claude" and debug["backend"] == "session"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_matches_win,expect_crop", [(True, True), (False, False)])
async def test_shadow_crop_applied_when_dimensions_match(
    srv, raw_matches_win, expect_crop
):
    """Shadow crop applied only when captured image size matches win.w x win.h."""
    from unittest.mock import MagicMock
    from galaius.desktop import CoordTransform, DesktopElement, DesktopWindow

    win_w, win_h = 820, 610
    shadow = CoordTransform(
        shadow_left=10, shadow_top=5, shadow_right=10, shadow_bottom=5
    )
    # Create PNG with dimensions that either match or don't match win size
    img_size = (win_w, win_h) if raw_matches_win else (win_w + 50, win_h + 50)
    img = PILImage.new("RGB", img_size, color="blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    test_png = buf.getvalue()

    mock_win = MagicMock(spec=DesktopWindow)
    mock_win.wid = 12345
    mock_win.w = win_w
    mock_win.h = win_h
    mock_win.name = "Test"
    mock_win.capture.return_value = test_png

    atspi_elements = [
        DesktopElement(index=1, role="button", name="OK", x=50, y=50, w=80, h=30)
    ]

    with (
        patch.object(det.CoordTransform, "from_xprop", return_value=shadow),
        patch.object(det.CoordTransform, "store"),
        patch.object(det.AtSpi, "detect_elements", return_value=atspi_elements),
        patch.object(srv, "_crop_image", wraps=srv._crop_image) as mock_crop,
    ):
        await srv._detect_desktop_elements(mock_win)

    if expect_crop:
        mock_crop.assert_called_once_with(test_png, 10, 5, win_w - 20, win_h - 10)
    else:
        mock_crop.assert_not_called()
