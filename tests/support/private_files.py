"""Making a private file readable by everyone, the way each system spells it (the attack a
private-file check must refuse), and asking whether a file is private."""

import subprocess
import sys
from pathlib import Path

from galaius.private_files import PRIVATE_FILES


def loosen(path: Path) -> None:
    """Let everyone read `path`, a folder with what it holds: Windows grants Everyone (S-1-1-0)
    read, inherited by a folder's contents; POSIX sets mode 0644, a folder 0755."""
    if sys.platform == "win32":
        grant = "*S-1-1-0:(OI)(CI)R" if path.is_dir() else "*S-1-1-0:R"
        subprocess.run(["icacls", str(path), "/grant", grant], check=True, capture_output=True)
    else:
        path.chmod(0o755 if path.is_dir() else 0o644)


def private(path: Path) -> bool:
    """Only this user can read `path`, as this system spells it (`PRIVATE_FILES`)."""
    try:
        PRIVATE_FILES.check(path)
    except PermissionError:
        return False
    return True
