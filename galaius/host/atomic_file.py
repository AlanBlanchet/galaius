"""A file replaced in one step: `replace_held_open(source, target)` renames a written temporary over `target`, also on
Windows while another handle has `target` open."""

import os
import time
from pathlib import Path

#: winerror of a rename refused only while another handle has the target open: ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION.
HELD_OPEN_WINERRORS = frozenset({5, 32})


def replace_held_open(source: Path, target: Path, *, seconds: float = 2.0) -> None:
    """`source` renamed over `target`. Windows refuses the rename for the instant another process or thread reads
    `target` (a supervisor reading the upgrade pointer; 2026-11-01 CI: two dispatch threads on one run's queue file,
    « [WinError 5] Access is denied »): tried again for up to `seconds`, then that refusal raised."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in HELD_OPEN_WINERRORS or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
