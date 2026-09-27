"""A JSON cache file that ages, shared by every live external source.

The model catalog (prices) and the benchmark board (scores) are the same thing twice: fetch from a
vendor, keep the answer in a fixed file under ``~/.interact/out/`` that the CLI and the VS Code
extension read DIRECTLY, and be honest about how old it is. They had that logic written out
separately — same TTL constant, same read, same write, same age arithmetic — so a fix to one
(making the write atomic) would silently have missed the other.

The path is fixed rather than following ``INTERACT_DEBUG_DIR``: two different front ends read it,
so it must not move.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

#: Prices and scores both move on the order of days, so half a day keeps a panel responsive
#: without ever being meaningfully behind. One constant, so the two sources cannot drift apart.
TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class TTLCache:
    """One cache file. Owns where it lives, how it is read, and how it is written."""

    filename: str
    ttl_seconds: float = TTL_SECONDS

    @property
    def path(self) -> Path:
        return Path.home() / ".interact" / "out" / self.filename

    def read(self) -> dict | None:
        """The stored payload, or None if it is missing or unreadable — never raises."""
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def write(self, payload: dict) -> None:
        """Replace the file ATOMICALLY; a failure to cache never fails the call.

        Several interact servers run at once (one per editor window) and each refreshes on
        startup, so this path has N writers. A truncating write caught mid-flight leaves a half
        file, and both readers treat a parse error as "no data" — the panel would silently empty
        itself. Writing a temp beside it and renaming makes every reader see one version or the
        other, never a partial one. The pid suffix keeps concurrent writers off each other's temp.
        """
        target = self.path
        temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(payload))
            os.replace(temp, target)  # atomic within a filesystem
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass  # nothing left to do; a stray temp must not raise into the caller


def age_of(fetched_at: float) -> float:
    """Seconds since a fetch, or infinity when it never happened."""
    return max(0.0, time.time() - fetched_at) if fetched_at else float("inf")
