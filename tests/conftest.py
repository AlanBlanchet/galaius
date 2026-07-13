import os
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def _load_repo_dotenv() -> None:
    """Load nearest `.env` via the shared CLI loader (override=False)."""
    from interact.config import load_dotenv_for_cli

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

    saved = (
        litellm.acompletion,
        litellm.completion,
        litellm.atranscription,
        litellm.transcription,
    )
    litellm.acompletion, litellm.completion = _ablocked, _blocked
    litellm.atranscription, litellm.transcription = _ablocked, _blocked
    try:
        yield
    finally:
        (
            litellm.acompletion,
            litellm.completion,
            litellm.atranscription,
            litellm.transcription,
        ) = saved


@pytest.fixture(autouse=True)
def _isolate_interact_logs(tmp_path):
    """Keep test artifacts out of the user's real ~/.interact/logs: route Debug dumps to a per-test
    tmp via the screenshot_dump_dir override, which survives config.refresh() (unlike a plain field
    set, which review_ui's refresh would reset). A test that needs the real dump path overrides it."""
    from interact.runtime import config

    saved = config.screenshot_dump_dir
    config.screenshot_dump_dir = tmp_path / "interact-debug"
    try:
        yield
    finally:
        config.screenshot_dump_dir = saved


@pytest.hookimpl(tryfirst=True)
def pytest_timeout_set_timer(item, settings):
    # pytest-timeout's thread-method timer crashes pytest 9's capture manager during teardown on
    # Windows: read_global_capture() asserts `_global_capturing is not None` even when NOTHING hangs
    # (all tests pass in ~40s), failing CI with exit 1 and blocking the release. Suppress its timer on
    # Windows only — this firstresult hook wins with a truthy return so pytest-timeout sets no timer
    # (its cancel is a safe no-op when none was set). Linux + macOS keep full hang-protection. See #73.
    if sys.platform == "win32":
        return True
    return None


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
