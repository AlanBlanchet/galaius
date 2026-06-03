import os
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def _load_repo_dotenv() -> None:
    """Load nearest `.env` via the shared CLI loader (override=False)."""
    from interact.dotenv_loader import load_dotenv_for_cli

    load_dotenv_for_cli()


@pytest.fixture(autouse=True)
def _block_real_vlm_calls(request):
    """Unit tests must NEVER make a real model/provider call. Such a call can hang forever
    on an interactive auth flow (e.g. litellm's chatgpt device-code poll) or slow network —
    which is exactly what stalled CI. Block litellm's entry points so any un-mocked path
    fails fast with a clear message instead of hanging. Integration tests opt out."""
    if "integration" in request.keywords:
        yield
        return
    import litellm

    def _blocked(*_a, **_k):
        raise RuntimeError(
            "real litellm call blocked in a unit test — mock analyze_media/the VLM, "
            "or mark the test `integration`"
        )

    async def _ablocked(*_a, **_k):
        _blocked()

    saved = litellm.acompletion, litellm.completion
    litellm.acompletion, litellm.completion = _ablocked, _blocked
    try:
        yield
    finally:
        litellm.acompletion, litellm.completion = saved


def pytest_collection_modifyitems(config, items):
    # integration → needs API keys + a real browser; desktop → needs a live Linux display.
    # Skipping these keeps the suite green on macOS/Windows (and headless CI), so the
    # cross-OS matrix exercises the OS-agnostic code without false failures.
    no_key = not os.environ.get("OPENAI_API_KEY")
    no_linux_display = sys.platform != "linux" or not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )
    skip_integration = pytest.mark.skip(reason="No OPENAI_API_KEY set")
    skip_desktop = pytest.mark.skip(reason="desktop tests need Linux + a live display")
    for item in items:
        if no_key and "integration" in item.keywords:
            item.add_marker(skip_integration)
        if no_linux_display and "desktop" in item.keywords:
            item.add_marker(skip_desktop)
