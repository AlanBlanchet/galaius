"""Runtime singletons. ``config`` is a *live* view of ``~/.interact/config.env``: that file is
the source of truth, so editing it (via the TUI, the VS Code extension, or by hand) is reflected
on the next tool call of a running server — no restart/reconnect needed. Previously the file was
snapshotted once into the environment at startup, so a long-lived MCP server kept stale models.
"""

from interact.config import Config
from interact.data import PackageData
from interact.formats import CoordFormat
from interact.models import CircuitBreaker, Model
from interact.userconfig import UserConfig


class _LiveConfig:
    """Proxy that resolves attributes against a :class:`Config` rebuilt from the *current*
    ``config.env`` + environment. ``refresh()`` re-reads the file; between refreshes the last
    build is reused (one tool call sees a consistent snapshot, not the file 10×).

    In-process attribute *sets* (tests, a ``screenshot_dump_dir`` override) are applied onto the
    inner Config object itself — so methods and computed properties that read ``self.field``
    (``model_for``, ``usage_log``, …) see them — and are re-applied after each refresh, so an
    explicit override keeps winning over the persisted file value.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_overrides", {})
        object.__setattr__(self, "_inner", Config())

    def refresh(self) -> "_LiveConfig":
        """Rebuild the inner Config from the live file (file values override the environment),
        then re-apply in-process overrides. Call once at the start of a tool invocation."""
        import os

        file_vars = UserConfig.read()
        # The file is authoritative for INTERACT_* settings: drop any INTERACT_* env var that the
        # file no longer defines, so clearing a setting in the file (e.g. a model) actually takes
        # effect on a running server — not left stale in the environment. Provider *_API_KEY vars
        # set in the real environment are left untouched (the file only seeds, never owns those).
        for name in [k for k in os.environ if k.startswith("INTERACT_") and k not in file_vars]:
            del os.environ[name]
        for name, value in file_vars.items():
            os.environ[name] = value  # file is source of truth → override, not setdefault
        inner = Config()
        for name, value in object.__getattribute__(self, "_overrides").items():
            setattr(inner, name, value)
        object.__setattr__(self, "_inner", inner)
        return self

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value) -> None:
        object.__getattribute__(self, "_overrides")[name] = value
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def clear_overrides(self) -> None:
        """Drop all in-process overrides and rebuild from the file (used by tests for isolation,
        so a transient override doesn't leak into later code/tests)."""
        object.__getattribute__(self, "_overrides").clear()
        self.refresh()


config = _LiveConfig()
breaker = CircuitBreaker()

CoordFormat.load_from_config(PackageData.models_data().get("coordFormats", {}))
Model.load_registry()
