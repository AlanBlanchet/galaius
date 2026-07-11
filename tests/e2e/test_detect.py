"""E2E: VLM element detection tests against real providers.

Uses :class:`interact.models.Model` directly — no provider/model classes
are defined here. LLM-visible artifacts (``interpretation.txt``,
``vlm_elements.json``) reference elements by index/name only; pixel
coordinates live in debug-only files (``vlm_raw.txt``, ``ground_truth.json``).
"""

import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from interact.desktop import atspi
from interact.browser import BrowserManager
from interact.desktop import DesktopElement, DesktopWindow
from interact.vision.detect import _vlm_detect_elements
from interact.models import Model
from interact.runtime import config
from interact.state import annotate_screenshot

from .harness import (
    OUT_DIR,
    Comparison,
    E2EResult,
    ResultCollector,
    cheapest_grounding_model,
    make_output_dir,
    save_artifact,
    should_skip,
)

TEST_PAGE = Path(__file__).parents[1] / "fixtures" / "test_page.html"
DOM_ELEMENTS_JS = (
    Path(__file__).parents[1] / "fixtures" / "dom_elements.js"
).read_text()


@pytest.fixture(scope="session", autouse=True)
def _setup_debug():
    config.screenshot_dump_dir = OUT_DIR


@pytest.fixture(scope="session")
def gtk_window():
    gui_path = Path(__file__).parents[1] / "fixtures" / "test_gui.py"
    proc = subprocess.Popen(
        [sys.executable, str(gui_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    win = None
    while time.time() < deadline:
        win = DesktopWindow.find("Interact Test")
        if win:
            break
        time.sleep(0.3)
    if not win:
        proc.terminate()
        pytest.skip("GTK test window did not appear (no display?)")
    yield win
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="session")
def ground_truth(gtk_window) -> list[DesktopElement]:
    time.sleep(0.5)
    elements = atspi.AtSpi.detect_elements("Interact Test")
    if not elements:
        pytest.skip("AT-SPI unavailable — cannot establish ground truth")
    return elements


@pytest.fixture(scope="session")
def screenshot(gtk_window) -> bytes:
    return gtk_window.capture()


@pytest.fixture(scope="session")
def screenshot_size(screenshot) -> tuple[int, int]:
    img = Image.open(io.BytesIO(screenshot))
    return img.width, img.height


class DetectionTest:
    """Base for VLM detection tests — shared annotation + comparison logic."""

    def _run_comparison(
        self,
        out_dir: Path,
        screenshot_bytes: bytes,
        vlm_elements: list[DesktopElement],
        truth_elements: list[DesktopElement],
        model: Model,
        test_name: str,
        elapsed: float,
        wall_time: float,
        results: ResultCollector,
    ) -> Comparison:
        comparison = Comparison.from_elements(vlm_elements, truth_elements)

        save_artifact(
            out_dir,
            "annotated_vlm",
            annotate_screenshot(screenshot_bytes, vlm_elements),
            "png",
        )
        save_artifact(
            out_dir,
            "annotated_gt",
            annotate_screenshot(screenshot_bytes, truth_elements),
            "png",
        )
        save_artifact(
            out_dir, "comparison", comparison.model_dump_json(indent=2), "json"
        )

        est_cost = (model.input_cost_per_million or 0) * 0.001
        interpretation = self._build_interpretation(
            comparison, model, elapsed, wall_time, est_cost
        )
        save_artifact(out_dir, "interpretation", "\n".join(interpretation), "txt")

        result = E2EResult(
            provider=model.provider,
            model=model.id,
            test_name=test_name,
            passed=comparison.passed,
            elapsed=elapsed,
            cost_estimate=est_cost,
            details=comparison.model_dump(),
            artifacts=[str(p) for p in out_dir.iterdir()],
        )
        results.add(result)

        print(
            f"\n  [{model.provider}] {model.id}"
            f"\n    detected={comparison.vlm_count} matched={comparison.matched_count}/{comparison.gt_count}"
            f" avg_off={comparison.avg_offset}px elapsed={elapsed:.1f}s"
            f" → {'PASS' if comparison.passed else 'FAIL'}"
        )
        return comparison

    def _build_interpretation(
        self,
        comparison: Comparison,
        model: Model,
        elapsed: float,
        wall_time: float,
        est_cost: float,
    ) -> list[str]:
        # Coordinate-free summary: name-only references are reusable by an
        # agent reading the file, raw pixel positions are not.
        return [
            f"Provider: {model.provider}",
            f"Model: {model.id}",
            f"Elements detected: {comparison.vlm_count} (ground truth: {comparison.gt_count})",
            f"Matched: {comparison.matched_count}/{comparison.gt_count} ({comparison.match_rate * 100:.0f}%)",
            f"Avg center offset: {comparison.avg_offset}px",
            f"Max center offset: {comparison.max_offset}px",
            f"Elapsed: {elapsed:.1f}s (wall: {wall_time:.1f}s)",
            f"Est. cost: ${est_cost:.6f}",
            f"Verdict: {'PASS' if comparison.passed else 'FAIL'}",
            "",
            "Matched elements (offset in px from ground-truth center):",
            *[f"  {m.name} ({m.offset}px)" for m in comparison.matched],
            "",
            "Unmatched ground-truth elements (nearest VLM box was further than tolerance):",
            *[f"  {u.name} (nearest {u.nearest}px)" for u in comparison.unmatched],
        ]


def _llm_safe_element_dump(elements: list[DesktopElement]) -> str:
    """JSON dump containing only role/name/index — never pixel coords.

    Coordinates remain in ``vlm_raw.txt`` and ``ground_truth.json`` for
    human debugging, but the LLM-facing artifact is index/name-only.
    """
    return json.dumps(
        [
            {"index": i + 1, "role": e.role, "name": e.name}
            for i, e in enumerate(elements)
        ],
        indent=2,
    )


def _debug_element_dump(elements: list[DesktopElement]) -> str:
    """Full dump including coords — for human debug only."""
    return json.dumps(
        [
            {
                "index": i + 1,
                "name": e.name,
                "role": e.role,
                "x": e.x,
                "y": e.y,
                "w": e.w,
                "h": e.h,
            }
            for i, e in enumerate(elements)
        ],
        indent=2,
    )


class TestDesktopDetection(DetectionTest):
    """Detect elements in GTK test window using real VLM calls."""

    @pytest.mark.parametrize("provider_idx", range(10))
    async def test_detect_elements(
        self,
        provider_idx: int,
        providers: list[str],
        ground_truth: list[DesktopElement],
        screenshot: bytes,
        screenshot_size: tuple[int, int],
        results: ResultCollector,
    ):
        if provider_idx >= len(providers):
            pytest.skip("no more providers")

        provider_name = providers[provider_idx]
        model = cheapest_grounding_model(provider_name)
        if not model:
            pytest.skip(f"{provider_name}: no grounding-capable model available")

        img_w, img_h = screenshot_size
        out_dir = make_output_dir(model.provider, model.id, "detect_elements")

        save_artifact(out_dir, "input", screenshot, "png")
        # Ground-truth coords kept for human debugging (debug_*).
        save_artifact(out_dir, "ground_truth_debug", _debug_element_dump(ground_truth), "json")
        save_artifact(out_dir, "ground_truth", _llm_safe_element_dump(ground_truth), "json")

        t0 = time.time()
        try:
            elements, elapsed, raw_text, used_model = await _vlm_detect_elements(
                screenshot,
                f"GTK test window — e2e test ({model.provider})",
                img_w,
                img_h,
                model_override=model.id,
                invocation_id=str(out_dir),
            )
        except Exception as exc:
            reason = should_skip(exc)
            if reason:
                results.add(
                    E2EResult(
                        provider=model.provider,
                        model=model.id,
                        test_name="detect_elements",
                        passed=False,
                        skipped=True,
                        skip_reason=reason,
                    )
                )
                save_artifact(out_dir, "skip_reason", f"{reason}\n{exc}", "txt")
                pytest.skip(reason)
            raise
        wall_time = time.time() - t0
        vlm_elements = elements or []

        if not vlm_elements and elapsed == 0:
            reason = "model returned 0 elements in 0s (API error)"
            save_artifact(out_dir, "error", f"{reason}\nRaw: {raw_text}", "txt")
            results.add(
                E2EResult(
                    provider=model.provider,
                    model=model.id,
                    test_name="detect_elements",
                    passed=False,
                    skipped=True,
                    skip_reason=reason,
                )
            )
            pytest.skip(f"{model.provider}/{model.id}: {reason}")

        save_artifact(out_dir, "vlm_raw", raw_text, "txt")
        save_artifact(out_dir, "vlm_elements_debug", _debug_element_dump(vlm_elements), "json")
        save_artifact(out_dir, "vlm_elements", _llm_safe_element_dump(vlm_elements), "json")

        comparison = self._run_comparison(
            out_dir,
            screenshot,
            vlm_elements,
            ground_truth,
            model,
            "detect_elements",
            elapsed,
            wall_time,
            results,
        )

        assert comparison.passed, (
            f"{model.provider}/{model.id}: match_rate={comparison.match_rate:.0%} "
            f"avg_offset={comparison.avg_offset}px (need >=40% matched, <=50px avg)\n"
            f"See: {out_dir}/interpretation.txt"
        )


class TestBrowserDetection(DetectionTest):
    """Detect elements in a headless browser page using real VLM calls."""

    async def test_detect_browser_elements(
        self,
        providers: list[str],
        results: ResultCollector,
    ):
        provider_name = providers[0]
        model = cheapest_grounding_model(provider_name)
        if not model:
            pytest.skip(f"{provider_name}: no grounding-capable model")

        out_dir = make_output_dir(model.provider, model.id, "detect_browser")

        browser_mgr = BrowserManager(config)
        try:
            await browser_mgr.ensure_ready()
            page = await browser_mgr.get_page()
            test_url = TEST_PAGE.resolve().as_uri()
            await page.goto(test_url)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(500)

            screenshot_bytes = await page.screenshot(type="png")
            img = Image.open(io.BytesIO(screenshot_bytes))
            img_w, img_h = img.width, img.height

            dom_raw = await page.evaluate(DOM_ELEMENTS_JS)
        finally:
            await browser_mgr.close()

        dom_elements = [
            DesktopElement(
                index=i + 1,
                x=e["x"],
                y=e["y"],
                w=e["w"],
                h=e["h"],
                role=e["role"],
                name=e["name"],
            )
            for i, e in enumerate(dom_raw)
        ]

        save_artifact(out_dir, "input", screenshot_bytes, "png")
        save_artifact(out_dir, "dom_elements_debug", json.dumps(dom_raw, indent=2), "json")
        save_artifact(out_dir, "dom_elements", _llm_safe_element_dump(dom_elements), "json")

        t0 = time.time()
        try:
            elements, elapsed, raw_text, used_model = await _vlm_detect_elements(
                screenshot_bytes,
                "E2E browser test page",
                img_w,
                img_h,
                model_override=model.id,
                invocation_id=str(out_dir),
            )
        except Exception as exc:
            reason = should_skip(exc)
            if reason:
                results.add(
                    E2EResult(
                        provider=model.provider,
                        model=model.id,
                        test_name="detect_browser",
                        passed=False,
                        skipped=True,
                        skip_reason=reason,
                    )
                )
                pytest.skip(reason)
            raise
        wall_time = time.time() - t0
        vlm_elements = elements or []

        if not vlm_elements and elapsed == 0:
            reason = "browser VLM returned 0 elements in 0s"
            results.add(
                E2EResult(
                    provider=model.provider,
                    model=model.id,
                    test_name="detect_browser",
                    passed=False,
                    skipped=True,
                    skip_reason=reason,
                )
            )
            pytest.skip(reason)

        save_artifact(out_dir, "vlm_raw", raw_text, "txt")
        save_artifact(out_dir, "vlm_elements_debug", _debug_element_dump(vlm_elements), "json")
        save_artifact(out_dir, "vlm_elements", _llm_safe_element_dump(vlm_elements), "json")

        comparison = self._run_comparison(
            out_dir,
            screenshot_bytes,
            vlm_elements,
            dom_elements,
            model,
            "detect_browser",
            elapsed,
            wall_time,
            results,
        )

        assert comparison.passed, (
            f"Browser {model.provider}/{model.id}: match_rate={comparison.match_rate:.0%} "
            f"avg_offset={comparison.avg_offset}px\nSee: {out_dir}/interpretation.txt"
        )
