"""galaius — browser + desktop automation for AI agents, over MCP.

``DIST_NAME`` is the single source of truth for the installed distribution name, so nothing
downstream (version banner, update check, feedback footer) hardcodes it.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

DIST_NAME = "galaius"


def installed_version() -> str:
    """Installed distribution version, or ``"0.0.0"`` when running from a source tree that
    was never installed. Never raises."""
    try:
        return _version(DIST_NAME)
    except PackageNotFoundError:
        return "0.0.0"


__version__ = installed_version()

USER_AGENT = f"{DIST_NAME}/{__version__}"
"""How this client names itself to a Galaius server. A request without it (the HTTP library's
default) is served the contract of clients released before responses were read tolerantly."""


def __getattr__(name: str):
    """Resolve `galaius.function` (the `@galaius.function` decorator) and `galaius.workflows`
    (the reverse-direction "run a server workflow from this machine" API) lazily, so a plain
    `import galaius` for `installed_version()` never pays for `galaius_core`/`httpx`."""
    if name == "function":
        from galaius.functions import function
        return function
    if name == "workflows":
        import importlib
        return importlib.import_module("galaius.workflows")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
