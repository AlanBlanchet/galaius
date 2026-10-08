"""A child process's environment built from nothing: what the operating system needs to start one,
plus what the test names."""

import os

#: Windows cannot start Python without SYSTEMROOT (its socket layer, loaded by `import asyncio`,
#: fails with WinError 10106) nor run `.cmd` / `.exe` lookups without COMSPEC / PATHEXT; TEMP / TMP
#: name its temporary folder. Absent on Linux, so nothing is copied there but PATH.
SYSTEM_VARIABLES = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def child_environment(**variables: str) -> dict[str, str]:
    """PATH and the system variables above from this process, then `variables` over them."""
    return {name: os.environ[name] for name in SYSTEM_VARIABLES if name in os.environ} | variables
