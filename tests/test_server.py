import io
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image as PILImage

import interact.detect as det
from interact.vision import VLMResult

_DESKTOP_CTX = "Desktop window: Test (800x600)"

_img = PILImage.new("RGB", (1, 1), color="red")
_buf = io.BytesIO()
_img.save(_buf, format="PNG")
_PNG = _buf.getvalue()
_VLM_JSON = '[{"role":"button","name":"Save","x":100,"y":200,"w":150,"h":30}]'


@pytest.fixture
def srv():
    import interact.server as _srv
    from interact.server import breaker

    breaker.clear()
    _srv.config.component_model = "test/component-model"
    with patch.object(_srv.Debug, "save"):
        yield _srv
    _srv.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()


@pytest.mark.asyncio
async def test_vlm_detect_elements_component_failure_returns_none(srv):
    """When component model is available but fails, no fallback to image — returns None."""
    fail = AsyncMock(side_effect=RuntimeError("missing API key"))
    with patch.object(srv, "_vlm", fail):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, _DESKTOP_CTX, 800, 600
        )

    assert fail.call_count == 1
    assert fail.call_args_list[0].kwargs.get("media_type") == "component"
    assert elements is None


@pytest.mark.asyncio
async def test_vlm_detect_elements_uses_only_component_model(srv):
    succeed = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.5))
    with patch.object(srv, "_vlm", succeed):
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
    with patch.object(srv, "_vlm", mock_vlm):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, _DESKTOP_CTX, 800, 600
        )

    assert mock_vlm.call_count == 1
    assert mock_vlm.call_args_list[0].kwargs.get("media_type") == "component"
    assert elements is None
    assert raw == ""


def test_element_detection_prompt_generic():
    from interact.formats import CoordFormat

    prompt = CoordFormat().prompt(1920, 1080)
    assert "pixel coordinates" in prompt
    assert '{"role":"button"' in prompt
    assert "box_2d" not in prompt
    assert "1920" in prompt
    assert "1080" in prompt


@pytest.mark.asyncio
async def test_vlm_detect_elements_fallback_uses_generic_prompt(srv):
    """When component model fails, it uses format-specific prompt."""
    from interact.formats import CoordFormat

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
        patch.object(srv, "_vlm", fail),
        patch.object(srv, "config") as mock_config,
    ):
        mock_config.resolve_model.side_effect = lambda role, *a, **k: (
            "gemini/gemini-2.0-flash" if role == "component" else "openai/gpt-4.1"
        )
        mock_config.vlm_max_dim = 1280
        mock_config.vlm_min_dim = 768
        await det._vlm_detect_elements(_PNG, _DESKTOP_CTX, 800, 600)

    # Gemini component model gets box_2d format prompt
    first_prompt = fail.call_args_list[0].args[2]
    assert "box_2d" in first_prompt
    CoordFormat.load_from_config({})


@pytest.mark.asyncio
async def test_circuit_breaker_skips_after_failure(srv):
    # First call: component fails, trips breaker
    fail = AsyncMock(side_effect=RuntimeError("key error"))
    with patch.object(srv, "_vlm", fail):
        await det._vlm_detect_elements(_PNG, "ctx", 800, 600)
    assert fail.call_count == 1

    # Second call: circuit tripped, only image model called (1 call)
    image_only = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.3))
    with patch.object(srv, "_vlm", image_only):
        await det._vlm_detect_elements(_PNG, "ctx", 800, 600)
    assert image_only.call_count == 1
    assert image_only.call_args.kwargs.get("media_type") == "image"


@pytest.mark.asyncio
async def test_circuit_breaker_resets_after_ttl(srv):
    from interact.server import breaker

    component_model = srv.config.model_for("component")
    # Trip the breaker with a timestamp in the past (beyond TTL)
    breaker.trip(component_model)
    breaker._trips[component_model] = time.monotonic() - breaker._ttl - 1

    mock_vlm = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.5))
    with patch.object(srv, "_vlm", mock_vlm):
        await det._vlm_detect_elements(_PNG, "ctx", 800, 600)

    # Should have tried component model (breaker reset) — only component runs
    assert mock_vlm.call_count == 1
    assert mock_vlm.call_args_list[0].kwargs.get("media_type") == "component"


@pytest.mark.asyncio
async def test_low_element_count_warning(srv, caplog):
    few_elements = '[{"role":"button","name":"OK","x":100,"y":200,"w":80,"h":30}]'
    mock_vlm = AsyncMock(return_value=VLMResult(text=few_elements, elapsed=0.5))
    with patch.object(srv, "_vlm", mock_vlm):
        import logging

        with caplog.at_level(logging.WARNING, logger="interact"):
            await det._vlm_detect_elements(_PNG, "ctx", 800, 600)

    assert any("Low element count" in r.message for r in caplog.records)


# --- _parse_vlm_elements crop-offset correction ---


def test_parse_vlm_elements_no_transform():
    """CoordFormat.parse returns raw VLM-space coords; caller applies CoordTransform."""
    from interact.formats import CoordFormat

    response = '[{"role":"button","name":"OK","x":200,"y":100,"w":80,"h":30}]'
    elements = CoordFormat().parse(response, 800, 600)
    assert elements is not None
    assert elements[0].x == 200
    assert elements[0].y == 100


# --- _UNSET sentinel in analyze_media ---


@pytest.mark.asyncio
async def test_unset_sentinel_uses_config_max_tokens():
    """When max_tokens=_UNSET (default), analyze_media uses config.max_tokens."""
    from interact.config import Config
    from interact.vision import MediaItem, _UNSET, analyze_media

    cfg = Config()
    media_item = [MediaItem(data="dGVzdA==", media_type="image", mime_type="image/png")]
    mock_completion = AsyncMock(return_value=VLMResult(text="ok", elapsed=0.1))
    # model is now resolved at the boundary and passed in; analyze_media no longer reads
    # config.model_for. It still validates the key, so patch that True.
    with (
        patch("interact.vision._vision_completion", mock_completion),
        patch(
            "interact.vision.litellm.validate_environment",
            return_value={"keys_in_environment": True},
        ),
    ):
        await analyze_media(media_item, "ctx", cfg, max_tokens=_UNSET, model="test-model")

    # Should have passed config.max_tokens (default: None)
    assert mock_completion.call_args.kwargs["max_tokens"] == cfg.max_tokens

    # Now pass explicit value -- should override
    with (
        patch("interact.vision._vision_completion", mock_completion),
        patch(
            "interact.vision.litellm.validate_environment",
            return_value={"keys_in_environment": True},
        ),
    ):
        await analyze_media(media_item, "ctx", cfg, max_tokens=512, model="test-model")

    assert mock_completion.call_args.kwargs["max_tokens"] == 512


# --- Session-timestamped debug folder structure ---


def test_debug_path_returns_none_without_invocation_id(srv):
    """Debug.path returns None when invocation_id is not set."""
    result = srv.Debug.path("my_label", "png")
    assert result is None


def test_debug_path_with_invocation_id(srv, tmp_path):
    """Debug.path with invocation_id uses flat filename inside invocation dir."""
    inv_dir = tmp_path / "20260423_140000" / "143045_tool"
    inv_dir.mkdir(parents=True)
    result = srv.Debug.path("vlm_raw", "txt", invocation_id=str(inv_dir))

    assert result is not None
    assert result.parent == inv_dir
    assert result.name == "vlm_raw.txt"


def test_new_invocation_dir(srv, tmp_path):
    """Debug.new_invocation_dir creates HHMMSS_tool subfolder under session timestamp, deduplicates on collision."""
    now = datetime(2026, 4, 23, 14, 52, 14)
    with (
        patch("interact.debug_utils._dt", wraps=datetime) as mock_dt,
        patch.object(srv.Debug, "SESSION_TS", "20260423_140000"),
    ):
        mock_dt.now.return_value = now
        inv = srv.Debug.new_invocation_dir(str(tmp_path), "get_interactive_elements")

    assert inv is not None

    p = Path(inv)
    assert p.name == "145214_get_interactive_elements"
    assert p.parent.name == "20260423_140000"
    assert p.exists()

    # Second call in same second creates _2 suffix
    with (
        patch("interact.debug_utils._dt", wraps=datetime) as mock_dt,
        patch.object(srv.Debug, "SESSION_TS", "20260423_140000"),
    ):
        mock_dt.now.return_value = now
        inv2 = srv.Debug.new_invocation_dir(str(tmp_path), "get_interactive_elements")
    assert Path(inv2).name == "145214_get_interactive_elements_2"
    assert Path(inv2).exists()


# --- Structured output ---


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
        patch.object(srv, "_vlm", mock_vlm),
        patch.object(det, "_model_supports_structured", return_value=True),
    ):
        with caplog.at_level(logging.WARNING, logger="interact"):
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
        patch.object(srv, "_vlm", mock_vlm),
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
        patch.object(srv, "_vlm", mock_vlm),
        patch.object(det, "_model_supports_structured", return_value=False),
    ):
        elements, elapsed, raw, _ = await det._vlm_detect_elements(
            _PNG, "ctx", 800, 600
        )

    for call in mock_vlm.call_args_list:
        assert call.kwargs.get("response_format") is None

    assert elements is not None


# --- _is_wm_only ---


def _desktop_el(name, role="push button", y=10):
    from interact.desktop import DesktopElement

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


# --- Debug.step_save ---


def test_step_debug_save_computes_step_dir(srv):
    with patch.object(srv.Debug, "save") as mock_save:
        srv.Debug.step_save(
            "/dbg/20260424/123000_run_actions",
            2,
            "click",
            "screenshot",
            b"png",
            ext="png",
        )

    mock_save.assert_called_once_with(
        "screenshot",
        b"png",
        ext="png",
        # os-native separator (Debug.step_save joins via pathlib → backslashes on Windows)
        invocation_id=str(Path("/dbg/20260424/123000_run_actions") / "002_click"),
    )


def test_step_debug_save_noop_without_invocation_id(srv):
    with patch.object(srv.Debug, "save") as mock_save:
        srv.Debug.step_save(None, 0, "click", "screenshot", b"data")

    mock_save.assert_not_called()


# --- CoordTransform resize integration in server ---


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
    with patch.object(srv, "_vlm", mock_vlm):
        elements, _, _, _ = await det._vlm_detect_elements(png, "ctx", 1920, 1080)

    assert elements is not None
    el = elements[0]
    # 640 * 1.5 = 960, 360 * 1.5 = 540, etc.
    assert el.x == 960
    assert el.y == 540
    assert el.w == 150
    assert el.h == 75


# --- model_override flow through _vlm_detect_elements ---


@pytest.mark.asyncio
async def test_vlm_detect_elements_model_override_bypasses_component(srv):
    """model_override skips component model and uses the override directly."""
    succeed = AsyncMock(return_value=VLMResult(text=_VLM_JSON, elapsed=0.3))
    with patch.object(srv, "_vlm", succeed):
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
@pytest.mark.parametrize("raw_matches_win,expect_crop", [(True, True), (False, False)])
async def test_shadow_crop_applied_when_dimensions_match(
    srv, raw_matches_win, expect_crop
):
    """Shadow crop applied only when captured image size matches win.w x win.h."""
    from unittest.mock import MagicMock
    from interact.desktop import CoordTransform, DesktopElement, DesktopWindow

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


# --- _vlm rate limit fallback ---


@pytest.mark.asyncio
async def test_vlm_rate_limit_triggers_fallback(srv):
    """RateLimitError in _vlm trips breaker and tries fallback model."""
    from litellm.exceptions import RateLimitError
    from interact.config import Config
    from interact.models import Model, ModelCapability, ModelChain
    from interact.server import breaker

    call_count = 0

    async def _mock_analyze(media, context, cfg, query=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RateLimitError("rate limited", "test", "test")
        return VLMResult(text="fallback response", elapsed=0.1, model="fallback/model")

    # Two-model chain: the primary now resolves at the boundary (resolve_model → first available
    # = primary/model), fails, and the breaker trips IT — then the chain advances to fallback.
    primary = Model(
        id="primary/model", provider="test", capabilities={ModelCapability.VLM}
    )
    fallback = Model(
        id="fallback/model", provider="test", capabilities={ModelCapability.VLM}
    )
    chain = ModelChain(role="image", preferences=[primary, fallback])

    with (
        patch("interact.server.analyze_media", _mock_analyze),
        patch.object(Config, "chain_for", return_value=chain),
        patch.object(Model, "is_available", return_value=True),
    ):
        result = await srv._vlm(_PNG, "test context", "test query")

    assert call_count == 2
    assert breaker.tripped("primary/model")
    assert "fallback" in result.text.lower()


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: __import__("litellm").exceptions.RateLimitError(
            "rate limited", "test", "test"
        ),
        lambda: __import__("litellm").exceptions.APIError(500, "boom", "test", "test"),
        lambda: ValueError("bad payload"),
    ],
    ids=["RateLimitError", "APIError", "ValueError"],
)
@pytest.mark.asyncio
async def test_vlm_falls_back_on_error(srv, make_error):
    """Any non-cancellation exception triggers fallback chain (not only RateLimitError)."""
    from interact.config import Config
    from interact.models import Model, ModelCapability, ModelChain

    call_count = 0

    async def _mock(media, context, cfg, query=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise make_error()
        return VLMResult(text="recovered", elapsed=0.1, model="fallback/model")

    primary = Model(
        id="primary/model", provider="test", capabilities={ModelCapability.VLM}
    )
    fallback = Model(
        id="fallback/model", provider="test", capabilities={ModelCapability.VLM}
    )
    chain = ModelChain(role="image", preferences=[primary, fallback])

    with (
        patch("interact.server.analyze_media", _mock),
        patch.object(Config, "chain_for", return_value=chain),
        patch.object(Model, "is_available", return_value=True),
    ):
        result = await srv._vlm(_PNG, "ctx")

    assert call_count == 2
    assert result.text.startswith("[Fallback: used fallback/model")
    assert "recovered" in result.text


# --- #61/#62: desktop record sessions (start/stop, not a forced 3s clip) --------------------


def _rec_win():
    """A mock desktop window with the record-session surface (#61)."""
    from unittest.mock import MagicMock
    from interact.desktop import DesktopWindow

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
    low = out.lower()
    assert "start=false" in low and "record" in low  # tells the agent how to stop


@pytest.mark.asyncio
async def test_record_desktop_stop_analyzes_the_session_clip(srv, monkeypatch):
    """#61: record(start=False) stops the open session, then analyzes its clip like any video."""
    import interact.desktop as dt

    win = _rec_win()
    win.stop_video.return_value = b"MP4DATA"
    monkeypatch.setattr(dt.Motion, "is_blank", staticmethod(lambda b: False))
    monkeypatch.setattr(dt.Motion, "detect", staticmethod(lambda b: True))

    async def fake_vlm(media, context, query, role, mime):
        return VLMResult(text="a token slides in", elapsed=0.1, model="m")

    monkeypatch.setattr(srv, "_vlm", fake_vlm)
    out = await srv._record_desktop(win, query="what animates?", start=False, duration=None, fps=None, path=None)
    win.stop_video.assert_called_once()
    assert "slides in" in out


@pytest.mark.asyncio
async def test_record_desktop_stop_without_a_session_explains(srv):
    """#61: stopping with no session open says so + names both ways forward, never crashes."""
    win = _rec_win()
    win.stop_video.return_value = None
    out = await srv._record_desktop(win, query=None, start=False, duration=None, fps=None, path=None)
    low = out.lower()
    assert "no recording" in low and "start=true" in low and "duration" in low


@pytest.mark.asyncio
async def test_record_desktop_explicit_duration_stays_a_one_shot_clip(srv, monkeypatch):
    """Backward compat (#62): an explicit duration= is still a blocking one-shot clip — never a
    session — so existing duration-based callers are unaffected."""
    import interact.desktop as dt

    win = _rec_win()
    win.capture_video.return_value = b"MP4"
    monkeypatch.setattr(dt.Motion, "is_blank", staticmethod(lambda b: False))
    monkeypatch.setattr(dt.Motion, "detect", staticmethod(lambda b: False))
    out = await srv._record_desktop(win, query=None, start=True, duration=2.0, fps=None, path=None)
    win.capture_video.assert_called_once()
    win.start_video.assert_not_called()
    assert "no motion" in out.lower()


# --- #57: get_interactive_elements(fresh=True) force-invalidates before detecting -----------


@pytest.mark.asyncio
async def test_get_interactive_elements_fresh_invalidates_the_cache_first(srv):
    """#57: fresh=True clears the window's accumulated element cache BEFORE detecting, so the
    returned refs reflect only the current frame — the recovery path for a stale cache."""
    from unittest.mock import AsyncMock, MagicMock
    from interact.desktop import DesktopElement, DesktopWindow

    win = MagicMock(spec=DesktopWindow)
    win.wid = 4242
    win.name = "aino"
    order: list[str] = []
    with (
        patch.object(srv, "_resolve_target", return_value=(win, None, None)),
        patch.object(DesktopElement, "invalidate", side_effect=lambda wid: order.append(f"invalidate:{wid}")),
        patch.object(srv, "_annotate_desktop", new=AsyncMock(side_effect=lambda *a, **k: order.append("detect") or ([], "report"))),
        patch.object(srv, "_desktop_label", return_value="aino"),
    ):
        await srv.get_interactive_elements(target="aino", fresh=True)
    assert order == ["invalidate:4242", "detect"]  # cleared, THEN detected


@pytest.mark.asyncio
async def test_get_interactive_elements_default_keeps_the_cache(srv):
    """Without fresh, the accumulating cache (the #19 same-screen union) is left intact."""
    from unittest.mock import AsyncMock, MagicMock
    from interact.desktop import DesktopElement, DesktopWindow

    win = MagicMock(spec=DesktopWindow)
    win.wid = 4242
    win.name = "aino"
    with (
        patch.object(srv, "_resolve_target", return_value=(win, None, None)),
        patch.object(DesktopElement, "invalidate") as inval,
        patch.object(srv, "_annotate_desktop", new=AsyncMock(return_value=([], "report"))),
        patch.object(srv, "_desktop_label", return_value="aino"),
    ):
        await srv.get_interactive_elements(target="aino")
    inval.assert_not_called()
