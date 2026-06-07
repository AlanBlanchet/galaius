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
