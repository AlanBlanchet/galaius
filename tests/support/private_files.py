"""Making a private file readable by everyone, the way each system spells it (the attack a
private-file check must refuse)."""

import subprocess
import sys
from pathlib import Path


def loosen(path: Path) -> None:
    """Let everyone read `path`: Windows grants Everyone (S-1-1-0) read; POSIX sets mode 0644."""
    if sys.platform == "win32":
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    else:
        path.chmod(0o644)
