"""Dev probes that exercise the core against a live page/window and log artifacts.

Two runs, both writing under ``out/tests/{session}/`` with one folder per step named
``{session}_{action}_{provider}_{model}``:

* :class:`DetectionProbe` — ``interact detect``: grounding only (``detect`` step).
* :class:`Scenario` — ``interact scenario``: the agent loop a model would drive —
  detect elements, then act on them *by their detected coordinates* (click, type,
  click), logging each step's before/after. Model selection is bound to the registry
  (:meth:`Model.recommended_grounding`); scoring reuses :class:`Comparison`.
"""

import asyncio
import io
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Self

from PIL import Image
from pydantic import BaseModel

from interact.desktop import atspi
from interact.desktop import DesktopElement, DesktopWindow
from interact.detect import _vlm_detect_elements, judge_missing_elements
from interact.models import Model, ModelCapability
from interact.state import annotate_screenshot

_REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_GUI = _REPO_ROOT / "tests" / "fixtures" / "test_gui.py"
TEST_PAGE = _REPO_ROOT / "tests" / "fixtures" / "test_page.html"
SCENARIO_PAGE = _REPO_ROOT / "tests" / "fixtures" / "scenario_page.html"
PANEL_GUI = _REPO_ROOT / "tests" / "fixtures" / "panel.py"


def _tk_python() -> str:
    """A Python whose tkinter can start a Tk() (uv's standalone Tk aborts under XCB)."""
    for exe in ("/usr/bin/python3", sys.executable):
        if exe and Path(exe).exists():
            probe = subprocess.run(
                [exe, "-c", "import tkinter; tkinter.Tk().destroy()"], capture_output=True
            )
            if probe.returncode == 0:
                return exe
    return sys.executable


class MatchedElement(BaseModel):
    name: str
    offset: float


class UnmatchedElement(BaseModel):
    name: str
    nearest: float


class Comparison(BaseModel):
    """Nearest-centre match of detected boxes against ground-truth elements."""

    vlm_count: int
    gt_count: int
    matched_count: int
    match_rate: float
    avg_offset: float | None
    max_offset: float | None
    matched: list[MatchedElement]
    unmatched: list[UnmatchedElement]

    @property
    def passed(self) -> bool:
        return self.match_rate >= 0.4 and (self.avg_offset or 999) <= 50.0

    @classmethod
    def from_elements(cls, vlm: list, truth: list, tolerance: float = 50.0) -> Self:
        matched: list[MatchedElement] = []
        unmatched: list[UnmatchedElement] = []
        for gt in truth:
            gc = (gt.x + gt.w // 2, gt.y + gt.h // 2)
            best = float("inf")
            for v in vlm:
                vc = (v.x + v.w // 2, v.y + v.h // 2)
                distance = ((gc[0] - vc[0]) ** 2 + (gc[1] - vc[1]) ** 2) ** 0.5
                best = min(best, distance)
            if best <= tolerance:
                matched.append(MatchedElement(name=gt.name, offset=round(best, 1)))
            else:
                unmatched.append(UnmatchedElement(name=gt.name, nearest=round(best, 1)))
        offsets = [m.offset for m in matched]
        return cls(
            vlm_count=len(vlm),
            gt_count=len(truth),
            matched_count=len(matched),
            match_rate=round(len(matched) / len(truth), 2) if truth else 0.0,
            avg_offset=round(sum(offsets) / len(offsets), 1) if offsets else None,
            max_offset=round(max(offsets), 1) if offsets else None,
            matched=matched,
            unmatched=unmatched,
        )


class Capture(BaseModel):
    """A screenshot to probe, with optional AT-SPI ground truth."""

    model_config = {"arbitrary_types_allowed": True}

    screenshot: bytes
    width: int
    height: int
    context: str
    ground_truth: list[DesktopElement] | None = None

    @classmethod
    def _of(cls, screenshot: bytes, context: str, ground_truth: list[DesktopElement] | None) -> Self:
        image = Image.open(io.BytesIO(screenshot))
        return cls(screenshot=screenshot, width=image.width, height=image.height,
                   context=context, ground_truth=ground_truth)

    @classmethod
    def from_window(cls, title: str) -> Self:
        window = DesktopWindow.find(title)
        if window is None:
            raise SystemExit(f"Window '{title}' not found")
        truth = None
        try:
            truth = atspi.AtSpi.detect_elements(title)
        except Exception:
            pass
        return cls._of(window.capture(), f"Desktop window: {title}", truth)

    @classmethod
    async def from_browser(cls, page_arg: str) -> Self:
        from interact.browser import BrowserManager  # heavy: pulls in Playwright
        from interact.config import Config

        page_path = Path(page_arg).resolve()
        if not page_path.exists():
            page_path = TEST_PAGE
        manager = BrowserManager(Config())
        try:
            tab = await manager.new_tab(f"file://{page_path}")
            page = await manager.get_page(tab)
            await page.wait_for_load_state("networkidle")
            screenshot = await page.screenshot(type="png")
        finally:
            await manager.close()
        return cls._of(screenshot, f"Browser page: {page_path.name}", None)

    @classmethod
    def from_test_window(cls) -> Self:
        process = cls._launch_test_gui()
        if process is None:
            raise SystemExit("Failed to launch GTK test window (no display?)")
        try:
            time.sleep(0.5)
            window = DesktopWindow.find("Interact Test")
            truth = None
            try:
                truth = atspi.AtSpi.detect_elements("Interact Test")
            except Exception:
                pass
            return cls._of(window.capture(), "GTK test window — interact detect", truth)
        finally:
            process.terminate()
            process.wait(timeout=5)

    @staticmethod
    def _launch_test_gui() -> subprocess.Popen | None:
        if not TEST_GUI.exists():
            return None
        process = subprocess.Popen(
            [sys.executable, str(TEST_GUI)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            if DesktopWindow.find("Interact Test"):
                return process
            time.sleep(0.3)
        process.terminate()
        return None


class ArtifactRun(BaseModel):
    """Base for runs that write under ``out/tests/{session}/``.

    Each step gets a self-describing flat folder ``{session}_{action}_{provider}_{model}``
    so artifacts sort by run and identify their action + model at a glance.
    """

    model_config = {"arbitrary_types_allowed": True}

    out_root: Path
    session_ts: str

    def dir_for(self, action: str, model: Model, step: int | None = None) -> Path:
        bare = model.id.rsplit("/", 1)[-1]  # drop provider prefix; it's already in the slug
        # Stamp at call time so steps sort by when they actually ran (the session
        # folder groups the run; the step index orders + disambiguates repeats).
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        stamp = f"{now}_{step:02d}" if step is not None else now
        slug = f"{stamp}_{action}_{model.provider}_{bare}"
        path = self.out_root / slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _save(out_dir: Path, name: str, data: str | bytes, ext: str = "txt") -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.{ext}"
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data)
        return path

    @staticmethod
    def _element_dump(elements: list[DesktopElement]) -> str:
        return json.dumps(
            [{"i": i + 1, "role": e.role, "name": e.name, "x": e.x, "y": e.y, "w": e.w, "h": e.h}
             for i, e in enumerate(elements)],
            indent=2,
        )

    @staticmethod
    def select_models(model: str | None, all_providers: bool) -> list[Model]:
        """Resolve which grounding model(s) to run (shared by detect + scenario)."""
        if model:
            match = Model.by_id(model) or next(
                (m for m in Model.registry() if m.id.endswith(model)), None
            )
            return [match or Model.from_litellm_id(model)]
        ranked = Model.recommended_grounding()
        if all_providers:
            per_provider: dict[str, Model] = {}
            for candidate in ranked:  # best-score first; first wins per provider
                per_provider.setdefault(candidate.provider, candidate)
            return list(per_provider.values())
        if ranked:
            return [ranked[0]]
        return Model.available_by_capability(ModelCapability.VLM)[:1]


class DetectionProbe(ArtifactRun):
    """``interact detect``: run grounding model(s) over a capture, log artifacts."""

    models: list[Model]

    @classmethod
    def build(cls, model: str | None, all_providers: bool, session_ts: str) -> Self:
        Model.load_registry()
        models = cls.select_models(model, all_providers)
        if not models:
            raise SystemExit("No grounding models available — check API keys (see `interact doctor`).")
        return cls(models=models, out_root=Path("out") / "tests" / session_ts, session_ts=session_ts)

    async def run(self, capture: Capture) -> None:
        print(f"Screenshot: {capture.width}×{capture.height}")
        if capture.ground_truth:
            print(f"Ground truth: {len(capture.ground_truth)} elements (AT-SPI)")
        for model in self.models:
            await self._run_one(model, capture)
        print(f"\nAll artifacts: {self.out_root}")

    async def _run_one(self, model: Model, capture: Capture) -> None:
        out_dir = self.dir_for("detect", model)
        self._save(out_dir, "input", capture.screenshot, "png")
        if capture.ground_truth:
            self._save(out_dir, "ground_truth", self._element_dump(capture.ground_truth), "json")

        print(f"\n{'─' * 60}\n  {model.id}  ({model.provider})  →  {out_dir.name}")
        start = time.time()
        try:
            elements, elapsed, raw_text, used_model = await _vlm_detect_elements(
                capture.screenshot, capture.context, capture.width, capture.height,
                model_override=model.id, invocation_id=str(out_dir),
            )
        except Exception as exc:
            self._save(out_dir, "error", f"{type(exc).__name__}: {exc}")
            print(f"  ERROR ({time.time() - start:.1f}s): {type(exc).__name__}: {exc}")
            return

        detected = elements or []
        self._save(out_dir, "vlm_raw", raw_text)
        self._save(out_dir, "vlm_elements", self._element_dump(detected), "json")
        self._save(out_dir, "annotated_vlm", annotate_screenshot(capture.screenshot, detected), "png")
        summary = [f"Model: {used_model or model.id}", f"Elements detected: {len(detected)}"]
        if capture.ground_truth:
            self._save(out_dir, "annotated_gt",
                       annotate_screenshot(capture.screenshot, capture.ground_truth), "png")
            comparison = Comparison.from_elements(detected, capture.ground_truth)
            self._save(out_dir, "comparison", comparison.model_dump_json(indent=2), "json")
            summary += [
                f"Matched: {comparison.matched_count}/{comparison.gt_count} "
                f"({comparison.match_rate * 100:.0f}%)",
                f"Avg offset: {comparison.avg_offset or 0:.1f}px",
            ]
            verdict = "PASS" if comparison.passed else "FAIL"
            print(f"  Detected {len(detected)} | matched {comparison.matched_count}/"
                  f"{comparison.gt_count} avg_off={comparison.avg_offset or 0:.1f}px → {verdict}")
        else:
            print(f"  Detected {len(detected)} elements (no ground truth)")
        cost = (model.input_cost_per_million or 0) * 0.001
        summary += [f"VLM time: {elapsed:.1f}s", f"Est. cost: ${cost:.6f}"]
        self._save(out_dir, "summary", "\n".join(summary))
        print(f"  {elapsed:.1f}s | ${cost:.6f}")

    @classmethod
    def acquire(cls, window: str | None, browser: bool, url: str | None) -> Capture:
        """Capture from a named window, a browser page, or the bundled GTK test window."""
        if window:
            return Capture.from_window(window)
        if url is not None:
            return asyncio.run(Capture.from_browser(url))
        if browser:
            return asyncio.run(Capture.from_browser(str(TEST_PAGE)))
        print("Launching GTK test window...")
        return Capture.from_test_window()


class Scenario(ArtifactRun):
    """``interact scenario``: simulate an agent loop on a browser page.

    For each model (the "agent"): screenshot → detect elements → act on them *by the
    detected coordinates* (click "Click Me", type into the input, click "Submit"),
    saving each step's before/after. This is what an LLM does after grounding — and
    it shows directly whether the boxes were accurate (an off box → a missed click).
    """

    models: list[Model]
    page_path: Path

    @classmethod
    def build(cls, model: str | None, all_providers: bool, session_ts: str, url: str | None) -> Self:
        Model.load_registry()
        models = cls.select_models(model, all_providers)
        if not models:
            raise SystemExit("No grounding models available — check API keys (see `interact doctor`).")
        path = Path(url).resolve() if url else SCENARIO_PAGE
        if not path.exists():
            path = SCENARIO_PAGE
        return cls(
            models=models,
            out_root=Path("out") / "tests" / session_ts,
            session_ts=session_ts,
            page_path=path,
        )

    async def run(self) -> None:
        from interact.browser import BrowserManager  # heavy: pulls in Playwright
        from interact.config import Config

        manager = BrowserManager(Config())
        try:
            tab = await manager.new_tab(f"file://{self.page_path}")
            page = await manager.get_page(tab)
            for model in self.models:
                await self._agent_loop(model, page)
        finally:
            await manager.close()
        print(f"\nAll artifacts: {self.out_root}")

    async def _agent_loop(self, model: Model, page) -> None:
        # Real action classes — what `run_actions` dispatches; deferred (pull Playwright).
        from interact.actions import (
            ClickAction, DragAction, HoverAction, KeyPressAction, ScrollAction,
        )

        await page.goto(f"file://{self.page_path}")  # reset state per agent
        await page.wait_for_load_state("networkidle")
        print(f"\n{'─' * 60}\n  agent: {model.id}  ({model.provider})")
        step = 1
        elements = await self._detect(model, page, step)
        step += 1
        if not elements:
            print("  detection returned nothing — skipping actions")
            return

        # Completeness loop: a judge inspects the annotated shot for missed components,
        # we re-detect focused on them and NMS-merge — up to detection_max_retries, with
        # no fixed target list (generalises to any UI).
        from interact.runtime import config as _config

        for _ in range(_config.detection_max_retries):
            shot = await page.screenshot(type="png")
            missing = await judge_missing_elements(shot, elements, model_override=model.id)
            judge_dir = self.dir_for("judge", model, step=step)
            step += 1
            self._save(judge_dir, "annotated",
                       annotate_screenshot(shot, elements, uniform_color="#00C000"), "png")
            self._save(judge_dir, "report", f"judge: missing {missing or 'none'}")
            print(f"  judge: missing {missing or 'none'}")
            if not missing:
                break
            before = len(elements)
            elements = await self._redetect(model, page, elements, missing, step)
            step += 1
            if len(elements) == before:
                break  # focused re-detect found nothing new — stop retrying

        def at(name: str) -> tuple[DesktopElement | None, int, int]:
            el = self._find(elements, name)
            return (el, el.x + el.w // 2, el.y + el.h // 2) if el else (None, 0, 0)

        clickme, cx, cy = at("Click Me")
        step = await self._step(model, page, step, "click", clickme, "Click Me",
                                lambda: ClickAction(x=cx, y=cy).execute(page))

        field, fx, fy = at("Enter text")

        async def _type() -> None:
            await ClickAction(x=fx, y=fy).execute(page)  # focus by detected coord
            await page.keyboard.type("hello from interact")

        step = await self._step(model, page, step, "type_text", field, "Enter text", _type)

        hover, hx, hy = at("Hover")
        step = await self._step(model, page, step, "hover", hover, "Hover Me",
                                lambda: HoverAction(x=hx, y=hy).execute(page))

        drag, sx, sy = at("Drag")
        drop, dx, dy = at("Drop")
        step = await self._step(model, page, step, "drag", drag and drop, "Drag→Drop",
                                lambda: DragAction(from_x=sx, from_y=sy, to_x=dx, to_y=dy, steps=8).execute(page))

        step = await self._step(model, page, step, "scroll", True, "down",
                                lambda: ScrollAction(direction="down", amount=6).execute(page))
        step = await self._step(model, page, step, "key_press", True, "End",
                                lambda: KeyPressAction(key="End").execute(page))

    async def _step(self, model: Model, page, step: int, action: str,
                    target: object, label: str, do) -> int:
        """Run one action: screenshot before/after, save artifacts, return next step.

        ``target`` is the detected element (or ``True`` for coordinate-less actions
        like scroll/key); a falsy target means detection missed it, so we skip.
        """
        out_dir = self.dir_for(action, model, step=step)
        self._save(out_dir, "before", await page.screenshot(type="png"), "png")
        if not target:
            self._save(out_dir, "report", f"{action} '{label}': not detected — skipped")
            print(f"  {step:02d} {action} '{label}': not detected, skipped")
            return step + 1
        report = f"{action} '{label}'"
        try:
            await do()
            await page.wait_for_timeout(150)
        except Exception as exc:
            report += f" — ERROR {type(exc).__name__}: {exc}"
        self._save(out_dir, "after", await page.screenshot(type="png"), "png")
        self._save(out_dir, "report", report)
        print(f"  {step:02d} {report}")
        return step + 1

    async def _detect(self, model: Model, page, step: int) -> list[DesktopElement]:
        shot = await page.screenshot(type="png")
        image = Image.open(io.BytesIO(shot))
        out_dir = self.dir_for("detect", model, step=step)
        self._save(out_dir, "input", shot, "png")
        try:
            elements, elapsed, _raw, _used = await _vlm_detect_elements(
                shot, f"browser scenario: {self.page_path.name}",
                image.width, image.height, model_override=model.id, invocation_id=str(out_dir),
            )
        except Exception as exc:
            self._save(out_dir, "error", f"{type(exc).__name__}: {exc}")
            print(f"  detect ERROR: {type(exc).__name__}: {exc}")
            return []
        elements = elements or []
        self._save(out_dir, "vlm_elements", self._element_dump(elements), "json")
        self._save(out_dir, "annotated", annotate_screenshot(shot, elements), "png")
        cost = (model.input_cost_per_million or 0) * 0.001
        self._save(out_dir, "summary", f"detected {len(elements)} elements, {elapsed:.1f}s, est ${cost:.6f}")
        print(f"  detect: {len(elements)} elements ({elapsed:.1f}s, ${cost:.6f})")
        return elements

    async def _redetect(self, model: Model, page, elements: list[DesktopElement],
                        missing: list[str], step: int) -> list[DesktopElement]:
        """Focused re-prompt for elements the first pass missed, merged via NMS.

        The model is told exactly what we're still looking for; new boxes are folded
        in with :meth:`DesktopElement.merge_keeping` (drops overlaps), so a second
        focused pass recovers misses without duplicating what we already have.
        """
        print(f"  re-detect: missing {missing} → focused re-prompt")
        shot = await page.screenshot(type="png")
        image = Image.open(io.BytesIO(shot))
        out_dir = self.dir_for("redetect", model, step=step)
        self._save(out_dir, "input", shot, "png")
        targets = ", ".join(f"'{m}'" for m in missing)
        try:
            found, _elapsed, _raw, _used = await _vlm_detect_elements(
                shot,
                f"List every interactive element on the page. Look carefully for and "
                f"include these in particular: {targets}.",
                image.width, image.height, model_override=model.id, invocation_id=str(out_dir),
            )
        except Exception as exc:
            self._save(out_dir, "error", f"{type(exc).__name__}: {exc}")
            print(f"  re-detect ERROR: {type(exc).__name__}: {exc}")
            return elements
        merged = DesktopElement.merge_keeping(elements, found or [])
        added = len(merged) - len(elements)
        still = [m for m in missing if self._find(merged, m) is None]
        self._save(out_dir, "vlm_elements", self._element_dump(merged), "json")
        self._save(out_dir, "annotated", annotate_screenshot(shot, merged), "png")
        self._save(out_dir, "report",
                   f"focused on {missing}: +{added} (now {len(merged)}); still missing: {still or 'none'}")
        print(f"  re-detect: +{added} elements (now {len(merged)}); still missing: {still or 'none'}")
        return merged

    @staticmethod
    def _find(elements: list[DesktopElement], target: str) -> DesktopElement | None:
        needle = target.lower()
        return next((e for e in elements if needle in (e.name or "").lower()), None)


class DesktopScenario(ArtifactRun):
    """``interact scenario --desktop``: the agent loop on a real OS panel via DesktopBackend.

    Launches a labelled Tk control panel in the configured target — the user's **local**
    session, or an isolated **nested** Xephyr display — captures the screen, runs
    grounding, then ACTS on the detected coordinates through the backend (click the
    buttons, type into the field) and checks each action against the panel's own recorded
    state. It proves the whole capture → detect → act loop on the desktop, the way
    :class:`Scenario` does for the browser. Detection is one VLM call per model; the
    actions and verification are free. ``nested`` is isolated and safe to run unattended;
    ``local`` drives the real cursor (and detects whatever else is on screen)."""

    models: list[Model]
    target: str

    @classmethod
    def build(cls, model: str | None, all_providers: bool, session_ts: str, target: str) -> Self:
        Model.load_registry()
        models = cls.select_models(model, all_providers)
        if not models:
            raise SystemExit("No grounding models available — check API keys (see `interact doctor`).")
        return cls(
            models=models,
            out_root=Path("out") / "tests" / session_ts,
            session_ts=session_ts,
            target=target,
        )

    async def run(self) -> None:
        from interact.desktop import LocalBackend, NestedBackend
        from interact.runtime import config

        if self.target == "nested":
            backend = NestedBackend(config.nested_display, config.nested_size)
        else:
            backend = LocalBackend()
        try:
            for model in self.models:
                await self._agent_loop(model, backend)
        finally:
            backend.close()
        print(f"\nAll artifacts: {self.out_root}")

    async def _agent_loop(self, model: Model, backend) -> None:
        state_path = self.out_root / f"panel_{model.provider}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}")
        proc = backend.spawn([_tk_python(), str(PANEL_GUI), str(state_path), "360x420+120+90"])
        print(f"\n{'─' * 60}\n  agent: {model.id}  ({model.provider})  target={self.target}")
        try:
            time.sleep(2.0)  # let Tk map + render in the target display
            elements = await self._detect(model, backend, 1)
            if not elements:
                print("  detection returned nothing — skipping actions")
                return
            step = 2
            for label in ("Click Me", "Increment", "Increment"):
                step = self._click(backend, elements, label, model, step, state_path)
            self._type(backend, elements, "hello interact", model, step, state_path)

            final = self._read_state(state_path)
            print(f"  final panel state: {final}")
        finally:
            proc.terminate()

    async def _detect(self, model: Model, backend, step: int) -> list[DesktopElement]:
        shot = backend.capture()
        image = Image.open(io.BytesIO(shot))
        out_dir = self.dir_for("detect", model, step=step)
        self._save(out_dir, "input", shot, "png")
        try:
            elements, elapsed, _raw, _used = await _vlm_detect_elements(
                shot, "desktop control panel: buttons (Click Me, Increment, Reset) and a text field",
                image.width, image.height, model_override=model.id, invocation_id=str(out_dir),
            )
        except Exception as exc:
            self._save(out_dir, "error", f"{type(exc).__name__}: {exc}")
            print(f"  detect ERROR: {type(exc).__name__}: {exc}")
            return []
        elements = elements or []
        self._save(out_dir, "vlm_elements", self._element_dump(elements), "json")
        self._save(out_dir, "annotated", annotate_screenshot(shot, elements), "png")
        cost = (model.input_cost_per_million or 0) * 0.001
        self._save(out_dir, "summary", f"detected {len(elements)} elements, {elapsed:.1f}s, est ${cost:.6f}")
        print(f"  detect: {len(elements)} elements ({elapsed:.1f}s, ${cost:.6f})")
        return elements

    def _click(self, backend, elements: list[DesktopElement], label: str,
               model: Model, step: int, state_path: Path) -> int:
        out_dir = self.dir_for("click", model, step=step)
        self._save(out_dir, "before", backend.capture_window("interact-panel"), "png")
        element = self._find(elements, label)
        if element is None:
            self._save(out_dir, "report", f"click '{label}': not detected — skipped")
            print(f"  {step:02d} click '{label}': not detected, skipped")
            return step + 1
        backend.click(element.x + element.w // 2, element.y + element.h // 2)
        backend.move(2, 2)  # leave the target so a repeat click isn't read as a double-click
        time.sleep(0.2)
        state = self._read_state(state_path)
        self._save(out_dir, "after", backend.capture_window("interact-panel"), "png")
        landed = state.get("last") == label or (label == "Increment" and state.get("count", 0) > 0)
        report = f"click '{label}' @ ({element.x + element.w // 2},{element.y + element.h // 2}) → state={state} ({'OK' if landed else 'no effect'})"
        self._save(out_dir, "report", report)
        print(f"  {step:02d} {report}")
        return step + 1

    def _type(self, backend, elements: list[DesktopElement], text: str,
              model: Model, step: int, state_path: Path) -> int:
        out_dir = self.dir_for("type_text", model, step=step)
        field = self._find(elements, "text") or self._find(elements, "enter") or self._find(elements, "type")
        if field is None:
            self._save(out_dir, "report", "type: text field not detected — skipped")
            print(f"  {step:02d} type: text field not detected, skipped")
            return step + 1
        backend.click(field.x + field.w // 2, field.y + field.h // 2)
        time.sleep(0.1)
        backend.type_text(text)
        time.sleep(0.2)
        state = self._read_state(state_path)
        landed = text.split()[0] in state.get("typed", "")
        report = f"type {text!r} into '{field.name}' → typed={state.get('typed')!r} ({'OK' if landed else 'no effect'})"
        self._save(out_dir, "report", report)
        print(f"  {step:02d} {report}")
        return step + 1

    @staticmethod
    def _find(elements: list[DesktopElement], target: str) -> DesktopElement | None:
        needle = target.lower()
        return next((e for e in elements if needle in (e.name or "").lower()), None)

    @staticmethod
    def _read_state(state_path: Path) -> dict:
        try:
            return json.loads(state_path.read_text() or "{}")
        except (json.JSONDecodeError, FileNotFoundError):
            return {}
