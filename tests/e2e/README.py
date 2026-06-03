"""E2E LLM testing harness — call real models, validate real outputs.

Run: uv run pytest tests/e2e/ -v -s
     uv run pytest tests/e2e/ -v -s -k openai
     uv run pytest tests/e2e/ -v -s --provider gemini

Skips gracefully when:
- API key not set
- Rate limit / budget exceeded
- Model not available

Outputs saved to: out/tests/{session}/e2e/{provider}/{test_name}/

VS Code extension E2E (status bar smoke):

    uv pip install -e ".[e2e]"
    INTERACT_E2E_VSCODE=1 uv run pytest tests/e2e/test_extension_statusbar.py -v

System tools required (Linux): ``code``, ``npx``, ``maim``, ``xdotool``,
``tesseract-ocr`` (system pkg). Python deps ``pytesseract`` + ``pillow`` come
from the ``e2e`` optional group. The test builds a VSIX, installs it into a
disposable user/extensions dir, screenshots the bottom 28px of the VS Code
window, upscales+binarizes the right 35%% strip, and OCRs for any substring
of "interact" (codicon fusion drops the leading "I"). Optional VLM tiebreaker
via ``INTERACT_IMAGE_MODEL`` env var.
"""
