"""interact — browser + desktop automation for AI agents, over MCP.

``DIST_NAME`` is the single source of truth for the installed distribution name, so nothing
downstream (version banner, update check, feedback footer) hardcodes it.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

DIST_NAME = "interact"


def installed_version() -> str:
    """Installed distribution version, or ``"0.0.0"`` when running from a source tree that
    was never installed. Never raises."""
    try:
        return _version(DIST_NAME)
    except PackageNotFoundError:
        return "0.0.0"


__version__ = installed_version()

USER_AGENT = f"{DIST_NAME}/{__version__}"
"""How this client names itself to an Interact server. A request without it (the HTTP library's
default) is served the contract of clients released before responses were read tolerantly."""


def __getattr__(name: str):
    """Resolve `interact.function` (the `@interact.function` decorator) and `interact.workflows`
    (the reverse-direction "run a server workflow from this machine" API) lazily, so a plain
    `import interact` for `installed_version()` never pays for `interact_core`/`httpx`."""
    if name == "function":
        from interact.functions import function
        return function
    if name == "workflows":
        import importlib
        return importlib.import_module("interact.workflows")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
