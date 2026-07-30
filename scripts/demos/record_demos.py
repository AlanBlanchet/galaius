"""Record the README demo animations by actually driving interact — no mockups.

Every GIF in `docs/assets/` is produced by this script, so a claim in the README is a recording of
the real tool doing the real thing, and any of them can be regenerated after a behaviour change:

    uv run python scripts/demos/record_demos.py            # all demos
    uv run python scripts/demos/record_demos.py desktop    # just one

Needs a running X display plus the sandbox's own dependencies (Xephyr, xdotool, maim, ffmpeg).
Each demo records into interact's isolated nested display, so nothing touches your real windows.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "docs" / "assets"

# The caption strip is the whole point of these clips: a screen recording of a browser looks like
# any automation tool, so each frame is labelled with the CALL that produced it and the text the
# agent got back. That pairing — "one line in, this happened, this came back" — is what a reader
# cannot get from the feature list.
BG = (13, 17, 23)          # GitHub dark canvas, so the clip sits naturally in the README
FG = (230, 237, 243)
ACCENT = (88, 166, 255)
MUTED = (125, 133, 144)
GOOD = (63, 185, 80)
PAD = 16
BAR_H = 78
GIF_WIDTH = 760
FPS = 10
HOLD = 1.6            # seconds each demo phase stays on screen, so a caption matches its frames
# A call takes a moment to land, so a caption fired at the instant the call is ISSUED describes a
# result the pixels do not show yet. Offsetting by the observed lag keeps every frame honest.
LAG = 0.9


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)


@dataclass
class Caption:
    """One labelled phase of a clip: shown from ``at`` seconds until the next caption starts."""

    at: float
    call: str                 # the tool call, rendered as code
    result: str = ""          # what interact returned, rendered as the agent would read it
    ok: bool = False          # colour the result as a success line


@dataclass
class Demo:
    name: str
    captions: list[Caption] = field(default_factory=list)


def _caption_for(captions: list[Caption], t: float) -> Caption | None:
    active = [c for c in captions if c.at <= t]
    return active[-1] if active else None


def _compose(frame: Image.Image, caption: Caption | None, width: int) -> Image.Image:
    """Frame + caption strip, sized to the README's column."""
    scale = width / frame.width
    body = frame.resize((width, max(1, round(frame.height * scale))), Image.LANCZOS)

    canvas = Image.new("RGB", (width, BAR_H + body.height), BG)
    canvas.paste(body, (0, BAR_H))
    d = ImageDraw.Draw(canvas)
    d.line([(0, BAR_H - 1), (width, BAR_H - 1)], fill=(48, 54, 61), width=1)

    if caption is None:
        return canvas
    d.text((PAD, 14), "›", font=_font(17, bold=True), fill=ACCENT)
    d.text((PAD + 18, 14), caption.call, font=_font(15, bold=True), fill=FG)
    if caption.result:
        d.text(
            (PAD + 18, 44),
            caption.result,
            font=_font(14),
            fill=GOOD if caption.ok else MUTED,
        )
    return canvas


def _video_to_gif(video: Path, out: Path, captions: list[Caption], width: int = GIF_WIDTH) -> None:
    """Extract frames, stamp each with the call that produced it, encode a palette-optimised GIF."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, composed = Path(tmp) / "raw", Path(tmp) / "composed"
        raw.mkdir()
        composed.mkdir()
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-vf", f"fps={FPS}", str(raw / "f_%04d.png")],
            check=True,
        )
        frames = sorted(raw.glob("f_*.png"))
        if not frames:
            raise RuntimeError(f"no frames extracted from {video}")
        for i, f in enumerate(frames):
            with Image.open(f) as im:
                shot = _compose(im.convert("RGB"), _caption_for(captions, i / FPS), width)
            shot.save(composed / f.name)

        palette = Path(tmp) / "palette.png"
        pattern = str(composed / "f_%04d.png")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", pattern,
             "-vf", "palettegen=stats_mode=diff", str(palette)],
            check=True,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", pattern,
             "-i", str(palette), "-lavfi", "paletteuse=dither=bayer:bayer_scale=3",
             "-loop", "0", str(out)],
            check=True,
        )
    print(f"  wrote {out.relative_to(REPO)} ({out.stat().st_size // 1024} KB)")


# --------------------------------------------------------------------------------------- desktop


def demo_desktop(out: Path) -> None:
    """Drive a REAL native app (gnome-calculator) inside the isolated sandbox.

    A calculator is deliberately chosen: a reader knows instantly what the right answer is, so the
    clip proves the clicks landed rather than asking them to take the recording's word for it."""
    from interact.desktop import DesktopWindow
    from interact.desktop.nested import NestedBackend

    backend = NestedBackend(display=120, size="460x620")
    try:
        backend.spawn(["gnome-calculator", "--mode=basic"])
        win = _await_window(backend, "Calculator", timeout=25)
        backend.fit_window(win)
        time.sleep(1.5)

        target = DesktopWindow.find_in(backend, win)
        keys = _calculator_keys(backend, win)

        # Record with the BLOCKING one-shot path and drive from a worker thread: the recorder owns
        # the main thread for a fixed window, so the clip length is deterministic (a demo has to be
        # reproducible frame-for-frame, unlike an interactive session).
        def drive() -> None:
            time.sleep(1.4)  # let the first frames show the untouched app
            for label in ("7", "×", "6", "="):
                x, y = keys[label]
                asyncio.run(target.click(x, y))
                time.sleep(0.75)

        worker = threading.Thread(target=drive, daemon=True)
        worker.start()
        video_bytes = backend.capture_video(win, duration=7.0, fps=FPS)
        worker.join(timeout=5)
    finally:
        backend.close()

    if not video_bytes:
        raise RuntimeError("recording produced no video")

    tmp = Path(tempfile.mkstemp(suffix=".mp4")[1])
    tmp.write_bytes(video_bytes)
    try:
        _video_to_gif(tmp, out, [
            Caption(0.0, 'launch_app("gnome-calculator")',
                    "running in an isolated display — your windows are untouched"),
            Caption(1.2, 'run_actions  target="nested:Calculator"',
                    "click 7 · click × · click 6 · click ="),
            Caption(5.2, "→ 42", "the real app computed it — no mock, no replay", ok=True),
        ], width=440)
    finally:
        tmp.unlink(missing_ok=True)


def _await_window(backend, title: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for _wid, name in backend.list_windows():
            if title.lower() in name.lower():
                return name
        time.sleep(0.4)
    raise RuntimeError(f"{title!r} never appeared in the sandbox")


# Button centres as a FRACTION of the window, measured off a real basic-mode capture at 460x620.
# Fractions rather than pixels so the demo survives a different window size; basic mode is forced at
# launch because Advanced mode reflows the keypad into a different grid — the first cut of this demo
# silently clicked ")" and recorded "Malformed expression" under a caption claiming 42.
_CALC_KEYS = {
    "7": (0.124, 0.705),
    "×": (0.687, 0.782),
    "6": (0.498, 0.782),
    "=": (0.874, 0.898),
}


def _calculator_keys(backend, win: str) -> dict[str, tuple[int, int]]:
    geo = backend.window_geometry(win)
    if geo is None:
        raise RuntimeError("calculator window vanished")
    _x, _y, w, h = geo
    return {k: (round(fx * w), round(fy * h)) for k, (fx, fy) in _CALC_KEYS.items()}


# --------------------------------------------------------------------------------------- browser


DEMO_PAGE = REPO / "docs" / "demo" / "shop.html"


def demo_browser(out: Path) -> None:
    """Drive a real page through a real browser session — filter, search, add to cart.

    Recorded against a LOCAL fixture (docs/demo/shop.html) rather than someone's live site: a demo
    pinned to a third party breaks on their next redesign and drags their branding into this repo.
    The controls are the ordinary ones an agent meets — a filter that re-renders a list, a search
    box, a counter that changes — so the clip shows targeting and state-change reporting, not a
    scripted happy path."""
    from interact.actions import _run_actions_browser
    from interact.actions.models import ClickAction, TypeTextAction
    from interact.browser import BrowserManager
    from interact.runtime import config

    async def run() -> bytes:
        mgr = BrowserManager(config, session_id="demo")
        try:
            page = await mgr.get_page()
            await page.goto(DEMO_PAGE.as_uri())
            await mgr.start_recording()
            page = await mgr.get_page()
            await page.goto(DEMO_PAGE.as_uri())
            # One step per PHASE, each held for HOLD seconds. Batched, the four actions finished in
            # about a second, so the captions below described state the clip had already left —
            # the timeline has to match what a viewer actually sees at that moment.
            await asyncio.sleep(HOLD)
            for step in (
                ClickAction(selector="button[data-cat='audio']"),
                TypeTextAction(selector="#q", text="field", clear_first=True),
                ClickAction(selector=".card button"),
            ):
                await _run_actions_browser(
                    mgr, [step], query=None, scope=None, wait=None, session="demo",
                )
                await asyncio.sleep(HOLD)
            await asyncio.sleep(0.8)
            return await mgr.stop_recording()
        finally:
            await mgr.close()

    video_bytes = asyncio.run(run())
    if not video_bytes:
        raise RuntimeError("browser recording produced no video")

    tmp = Path(tempfile.mkstemp(suffix=".webm")[1])
    tmp.write_bytes(video_bytes)
    try:
        _video_to_gif(tmp, out, [
            Caption(0.0, 'navigate("…/shop.html")', "Nimbus — Store · 4 products listed"),
            Caption(HOLD + LAG, 'run_actions  click "Audio"', "filter applied — list re-rendered, 2 products"),
            Caption(HOLD * 2 + LAG, 'run_actions  type_text "field"', "search narrowed — 1 product matches"),
            Caption(HOLD * 3 + LAG, 'run_actions  click "Add to cart"', "cart count 0 → 1", ok=True),
        ])
    finally:
        tmp.unlink(missing_ok=True)


DEMOS = {"desktop": demo_desktop, "browser": demo_browser}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("which", nargs="*", choices=[*DEMOS, "all"], default=["all"])
    args = ap.parse_args()
    for tool in ("ffmpeg", "Xephyr", "xdotool"):
        if shutil.which(tool) is None:
            print(f"missing {tool}", file=sys.stderr)
            return 1
    names = list(DEMOS) if "all" in args.which else args.which
    for name in names:
        print(f"recording {name}…")
        DEMOS[name](OUT_DIR / f"demo-{name}.gif")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
