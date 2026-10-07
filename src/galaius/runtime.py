"""Runtime settings refresh once per tool invocation. Connected portable preferences come
from the server or its verified stale cache; machine settings remain local.
"""

import os

from galaius.config import Config, UserConfig
from galaius.data import PackageData
from galaius.formats import CoordFormat
from galaius.models import CircuitBreaker, Model
from galaius.server_tool_settings import PORTABLE_ENV


class _LiveConfig:
    """Proxy that resolves attributes against a :class:`Config` rebuilt from the *current*
    server preferences + local settings. ``refresh()`` re-reads them; between refreshes the last
    build is reused (one tool call sees a consistent snapshot, not the file 10×).

    In-process attribute *sets* (tests, a ``screenshot_dump_dir`` override) are applied onto the
    inner Config object itself — so methods and computed properties that read ``self.field``
    (``model_for``, ``usage_log``, …) see them — and are re-applied after each refresh, so a
    local override keeps winning. Connected portable preferences cannot be overridden.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_overrides", {})
        object.__setattr__(self, "_file_owned", set())  # non-GALAIUS_ keys we applied from the file
        object.__setattr__(self, "_file_galaius_owned", set())
        object.__setattr__(self, "_process_galaius_env", UserConfig.process_galaius_env())
        object.__setattr__(self, "_inner", Config())

    def refresh(self) -> "_LiveConfig":
        """Rebuild Config from current effective settings, then re-apply local overrides. Call once at the start of a tool invocation."""
        file_vars = UserConfig.read()
        connected = UserConfig.server() is not None
        if connected:
            for name in PORTABLE_ENV:
                os.environ.pop(name, None)
        # A launcher (notably VS Code) supplies GALAIUS_* settings in the process environment.
        # Standalone config.env may override these; removing its override reveals the launch
        # value again. Connected portable preferences never restore old launch pins. Only keys previously introduced by the file are removed; unrelated spawn
        # settings are never swept merely because config.env does not mention them.
        file_galaius = {name for name in file_vars if name.startswith("GALAIUS_")}
        previous_galaius = object.__getattribute__(self, "_file_galaius_owned")
        process_galaius = object.__getattribute__(self, "_process_galaius_env")
        for name in previous_galaius - file_galaius:
            if name in process_galaius and not (connected and name in PORTABLE_ENV):
                os.environ[name] = process_galaius[name]
            else:
                os.environ.pop(name, None)
        object.__setattr__(self, "_file_galaius_owned", file_galaius)
        # Provider *_API_KEY vars: the file also OWNS the ones it defines (it overrides them below),
        # so when one is cleared from the file it must be dropped from the environment too — else a
        # long-lived server keeps authenticating with it and it LEAKS into sandbox child processes.
        # Track which we applied so a key from the real shell env (never file-defined) stays.
        file_owned = {k for k in file_vars if not k.startswith("GALAIUS_")}
        for name in object.__getattribute__(self, "_file_owned") - file_owned:
            os.environ.pop(name, None)
        object.__setattr__(self, "_file_owned", file_owned)
        for name, value in file_vars.items():
            os.environ[name] = value  # effective settings override old launcher values
        inner = Config()
        for name, value in object.__getattribute__(self, "_overrides").items():
            if not connected or name not in PORTABLE_ENV.values():
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
