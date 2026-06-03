"""End-to-end status bar test.

Opt-in via ``INTERACT_E2E_VSCODE=1``. Builds the VSIX, installs it into a
disposable user/extensions dir, launches VS Code in a fresh window, and OCRs
the status bar to assert that the word "interact" is visible. Saves the
captured strip and OCR output to ``out/tests/{session}/e2e/`` on failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXT_DIR = REPO_ROOT / "vscode-extension"

pytestmark = pytest.mark.skipif(
    os.environ.get("INTERACT_E2E_VSCODE") != "1",
    reason="opt-in: set INTERACT_E2E_VSCODE=1",
)


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        pytest.fail(f"required tool not on PATH: {tool}")
    return path


@pytest.fixture(scope="module")
def vsix(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("vsix")
    subprocess.run(["npm", "run", "compile"], cwd=EXT_DIR, check=True)
    pkg = out / "interact.vsix"
    subprocess.run(
        ["npx", "vsce", "package", "--no-yarn", "--out", str(pkg)],
        cwd=EXT_DIR,
        check=True,
    )
    return pkg


def _launch_vscode(tmp_path: Path, vsix: Path) -> subprocess.Popen:
    code = _require("code")
    data = tmp_path / "data"
    ext = tmp_path / "ext"
    data.mkdir()
    ext.mkdir()
    subprocess.run(
        [
            code,
            "--user-data-dir",
            str(data),
            "--extensions-dir",
            str(ext),
            "--install-extension",
            str(vsix),
        ],
        check=True,
    )
    return subprocess.Popen(
        [
            code,
            "--user-data-dir",
            str(data),
            "--extensions-dir",
            str(ext),
            "--new-window",
            "--disable-workspace-trust",
            str(tmp_path),
        ]
    )


# Codicon glyph ($(eye)) sits flush against the leading "I" of "Interact" and
# OCR commonly fuses them into a single glyph ("F", "B", "P" etc.), dropping
# the "I". We accept any suffix-substring of "interact" as a positive match.
_FUZZY_NEEDLES = ("interact", "nteract", "teract", "ntera")


def _preprocess(strip):
    """Upscale, grayscale, binarize for cleaner OCR of small UI text."""
    from PIL import Image

    w, h = strip.size
    big = strip.resize((w * 4, h * 4), Image.LANCZOS)
    gray = big.convert("L")
    # Simple threshold: status bar text is light on dark, so invert via point.
    bw = gray.point(lambda p: 0 if p < 140 else 255)
    return bw


def _ocr(img):
    import pytesseract

    return pytesseract.image_to_string(img, config="--psm 7")


def _vlm_fallback(strip_path: Path) -> bool | None:
    """Optional VLM tiebreaker. Returns True/False, or None if not configured."""
    model = os.environ.get("INTERACT_IMAGE_MODEL")
    if not model:
        return None
    try:
        import base64

        import litellm
    except ImportError:
        return None
    b64 = base64.b64encode(strip_path.read_bytes()).decode()
    resp = litellm.completion(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Does this status bar contain the word 'Interact' "
                            "(case-insensitive)? Answer YES or NO."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
    )
    answer = resp.choices[0].message.content.strip().upper()
    return answer.startswith("YES")


def test_status_bar_shows_interact(tmp_path, vsix):
    pytest.importorskip("pytesseract")
    PIL = pytest.importorskip("PIL.Image")
    _require("maim")
    _require("xdotool")

    proc = _launch_vscode(tmp_path, vsix)
    try:
        time.sleep(6)
        wid = (
            subprocess.check_output(
                ["xdotool", "search", "--name", "Visual Studio Code"]
            )
            .decode()
            .splitlines()[0]
        )
        png = tmp_path / "shot.png"
        subprocess.run(
            ["maim", "-i", wid, str(png)],
            check=True,
        )
        img = PIL.open(png)
        w, h = img.size
        # Status bar strip = bottom 28px. Our item is right-aligned, so OCR
        # only the rightmost 35% to drop noisy git/launchpad text on the left.
        strip = img.crop((int(w * 0.65), h - 28, w, h))
        processed = _preprocess(strip)
        text = _ocr(processed)
        lower = text.lower()
        if any(needle in lower for needle in _FUZZY_NEEDLES):
            print(f"[statusbar] OCR matched: {text!r}")
            return

        # Save artifacts for debugging before attempting VLM fallback.
        from .harness import OUT_DIR
        out_dir = OUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        strip_path = out_dir / f"statusbar_FAIL_{ts}.png"
        strip.save(strip_path)
        processed.save(out_dir / f"statusbar_FAIL_{ts}_processed.png")
        (out_dir / f"statusbar_FAIL_{ts}.txt").write_text(text)

        vlm = _vlm_fallback(strip_path)
        if vlm is True:
            print(f"[statusbar] OCR miss {text!r}; VLM confirmed Interact")
            return
        pytest.fail(
            f"status bar OCR did not contain 'interact'; got: {text!r}"
            + (" (VLM said NO)" if vlm is False else "")
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
