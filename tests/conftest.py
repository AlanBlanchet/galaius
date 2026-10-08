import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

# Set BEFORE any galaius module is imported: importing `galaius.runtime` loads the model
# registry, which now asks a running Ollama daemon what it has. A unit test must never reach the
# network — and on a developer box with a real daemon it silently would. `setdefault` so an
# integration run can force it back on.
#
# The name is deliberately NOT `GALAIUS_`-prefixed: `_LiveConfig.refresh()` deletes every
# `GALAIUS_*` var that config.env does not define, and any test whose code path refreshes config
# (every `@instrumented` MCP tool does) would silently re-enable discovery for the REST of the run.
os.environ.setdefault("OLLAMA_DISCOVERY", "0")

# Same reason, same timing: the registry is rescored from the live Artificial Analysis board AT
# LOAD, and importing `galaius.runtime` loads it before any fixture can run. A developer box with
# a real fetch would give the suite different scores from CI — and did. Empty means "no board"; a
# test that wants one passes its own path to `live_scores`.
os.environ.setdefault("BENCHMARK_SCORES", "")
# Avoid LiteLLM fetching its price table at import; explicit HTTP fixtures may opt back in.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
# Unit tests retain the historical mocked-LiteLLM default.  Production Config defaults to the
# subscription session path; the explicit test override prevents an old test that patches only
# `_vision_completion` from launching the user's real Claude/Codex login by accident.
os.environ.setdefault("GALAIUS_MEDIA_BACKEND", "api")
os.environ.setdefault("GALAIUS_MEDIA_BILLING", "api_allowed")

from galaius.config import UserConfig, load_dotenv_for_cli
from galaius.agents.providers import AgentProvider, ClaudeCodeProvider
from galaius.models import Model
from galaius.pinned_directory import PinnedDirectory

# Playwright's own browser cache, read before any test moves HOME: a unit test's HOME is a fresh
# folder, and a browser looked up there is downloaded again for every browser test (minutes each,
# and on CI one such download eventually never ends).
PLAYWRIGHT_BROWSERS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright" if sys.platform == "win32"
    else Path.home() / "Library" / "Caches" / "ms-playwright" if sys.platform == "darwin"
    else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"
)

# Importing `galaius.cli` applies `UserConfig.PATH` to `os.environ` at import, i.e. while test
# modules are collected, before any fixture runs. Left at the developer's own config file, its
# provider keys reached every unit test for the rest of the run, so a test needing a configured
# model passed on a developer computer and failed on every CI runner. Collection reads no one's file.
UserConfig.PATH = Path(__file__).resolve().parents[1] / "out" / "tests" / "no-user-config" / "config.env"


@pytest.fixture
def media_output_root() -> Path:
    """Unique private media output outside global /tmp, including repeated/concurrent runs."""
    parent = Path(__file__).resolve().parents[1] / "out" / "tests" / "media"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix="case-") as directory:
        yield Path(directory)


@pytest.fixture
def desktop_gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the Linux desktop-resolution path for a display-free unit test that mocks the backend:
    force ``desktop_supported()`` True (so a mac/win runner doesn't take the portable-screen
    branch) and open the unsupported gate. Opt in by requesting this fixture by name — it used to
    be an autouse fixture hidden inside ``tests/support/desktop.py`` that three files turned on
    for themselves merely by importing its name (a live-by-import trick pytest allows but that
    hides which tests actually need it); the off-Linux behaviour has its own coverage in
    test_cross_platform.py."""
    from galaius import server as srv

    monkeypatch.setattr("galaius.desktop.backend.desktop_supported", lambda: True)
    monkeypatch.setattr(srv.targets, "_desktop_unsupported", lambda *a, **k: None)


@pytest.fixture
def provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """One configured provider key (OpenAI's, a fake value): a role resolves against the bundled
    catalog. With no key at all a role refuses to resolve (`Config.resolve_model`), as on a fresh
    computer and on every CI runner."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


@pytest.fixture(params=PinnedDirectory.backends(), ids=lambda backend: backend.__name__)
def directory_backend(request: pytest.FixtureRequest):
    """Run on every file-system backend this computer has (`PinnedDirectory.backends`): POSIX
    descriptors and the Windows full-path walk, so the Windows code runs on Linux too. Opt in by
    name, or `pytestmark = pytest.mark.usefixtures("directory_backend")`."""
    with PinnedDirectory.using(request.param):
        yield request.param


@pytest.fixture(autouse=True)
def _isolate_unit_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Default unit authority is temporary; explicit test fixtures may override it afterward.

    HOME alone cannot relocate UserConfig.PATH, which was captured at module import.
    Integration tests explicitly retain their configured environment and dotenv behaviour.
    """
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(tmp_path / ".galaius" / "out" / "agents"))
    monkeypatch.setenv("GALAIUS_AGENT_CEILING", "false")  # never reshape the real agent slice
    if "integration" in request.keywords:
        load_dotenv_for_cli()
        return
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", PLAYWRIGHT_BROWSERS)
    for key, directory in (
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_STATE_HOME", ".local/state"),
    ):
        monkeypatch.setenv(key, str(tmp_path / directory))
    for key in ("GALAIUS_PARENT_RUN_ID", "GALAIUS_RUN_ID", "GALAIUS_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / ".galaius" / "config.env")
    monkeypatch.setattr(UserConfig, "_process_galaius_env", None)


@pytest.fixture(autouse=True)
def _default_unit_media_to_mocked_api(monkeypatch):
    """Re-apply after every live-config refresh, which intentionally clears file-absent env keys."""
    monkeypatch.setenv("GALAIUS_MEDIA_BACKEND", "api")
    monkeypatch.setenv("GALAIUS_MEDIA_BILLING", "api_allowed")


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
        litellm.acreate_file,
        litellm.atranscription,
        litellm.transcription,
    )
    litellm.acompletion, litellm.completion = _ablocked, _blocked
    litellm.acreate_file = _ablocked
    litellm.atranscription, litellm.transcription = _ablocked, _blocked
    try:
        yield
    finally:
        (
            litellm.acompletion,
            litellm.completion,
            litellm.acreate_file,
            litellm.atranscription,
            litellm.transcription,
        ) = saved


@pytest.fixture(autouse=True)
def _block_real_subscription_cli(monkeypatch, request):
    """A unit test may use a fake executable, but may never spend the user's plan allowance."""
    if "integration" in request.keywords:
        return
    real = {
        Path(path).resolve()
        for name in ("claude", "codex")
        if (path := shutil.which(name)) is not None
    }
    original = AgentProvider.subscription_authenticated
    original_process = ClaudeCodeProvider.run_media_process

    async def guarded(self, env, *, timeout=10):
        executable = Path(self.executable()).resolve()
        if executable in real:
            raise RuntimeError(
                "real subscription CLI blocked in a unit test — use a fake executable"
            )
        return await original(self, env, timeout=timeout)

    async def guarded_process(self, argv, **kwargs):
        if argv and Path(argv[0]).resolve() in real:
            raise RuntimeError(
                "real subscription CLI process blocked in a unit test — use a fake executable"
            )
        return await original_process(self, argv, **kwargs)

    monkeypatch.setattr(AgentProvider, "subscription_authenticated", guarded)
    monkeypatch.setattr(ClaudeCodeProvider, "run_media_process", guarded_process)


@pytest.fixture(autouse=True)
def _isolate_galaius_logs(tmp_path):
    """Keep test artifacts out of the user's real ~/.galaius/logs: route Debug dumps to a per-test
    tmp via the screenshot_dump_dir override, which survives config.refresh() (unlike a plain field
    set, which review_ui's refresh would reset). A test that needs the real dump path overrides it."""
    from galaius.runtime import config

    saved = (config.screenshot_dump_dir, config.media_backend, config.media_billing)
    config.screenshot_dump_dir = tmp_path / "galaius-debug"
    config.media_backend = "api"
    config.media_billing = "api_allowed"
    try:
        yield
    finally:
        config.screenshot_dump_dir, config.media_backend, config.media_billing = saved


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

@pytest.fixture
def reset_model_registry():
    """`Model._reset()` before and after — the shared body for a file whose OWN tests want a
    private, empty `Model` catalog per test. Not autouse here: a file opts in with its own
    `@pytest.fixture(autouse=True)` wrapper (see `test_probe.py`, `test_cli_reports.py`), so this
    stays scoped to the files that actually want it rather than resetting the registry around
    every test in the suite. `test_models.py`'s `_clear_registry` also clears measured
    Benchmark scores and stays a separate, file-local fixture — a real superset, not this."""
    Model._reset()
    yield
    Model._reset()


@pytest.fixture
def http_origin(tmp_path):
    """A real http origin serving one static page, for anything the browser keys by ORIGIN —
    localStorage above all: a `data:` / `about:blank` page has an opaque origin, so state set there
    never survives anything and a test on it proves nothing. Yields the origin (no trailing slash)."""
    (tmp_path / "index.html").write_text("<title>origin</title><body>served</body>")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _forget_desktop_caches() -> None:
    """Empty the process-wide window caches between tests.

    `CoordTransform` and `DesktopElement` keep what they learn about a window in module-level
    dicts keyed by window id, and every desktop test builds its window with the same id (123).
    One test storing decoration offsets for that id therefore moved another file's pointer by
    26 pixels, which is only visible when the two run in the same session — ten failures in the
    full suite, green file by file. Clearing here fixes the class, not the pair.
    """
    from galaius.desktop import coords, element

    coords._coord_cache.clear()
    element._element_cache.clear()
    yield
    coords._coord_cache.clear()
    element._element_cache.clear()
