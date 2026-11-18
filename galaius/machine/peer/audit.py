"""One line on THIS PC for every run another of its owner's PCs placed here (the server's audit names the owner; this names what ran on this machine)."""

import json
import os
import time
from uuid import UUID

from galaius.host.paths import UserPaths


class PeerAudit:
    @staticmethod
    def path():
        return UserPaths.config() / "peer-audit.log"

    @classmethod
    def started(cls, run_id: UUID, role: str | None, permission: str | None, asked_by_machine: UUID, asked_by_name: str, asked_by_run: UUID | None, verified: bool,
                fenced: bool, project: UUID | None) -> None:
        """Appended when the run exists; never raises (a full disk never stops a run that already started)."""
        line = {"at": time.time(), "run": str(run_id), "role": role, "permission": permission, "from_pc": str(asked_by_machine), "from_name": asked_by_name,
                "from_run": str(asked_by_run) if asked_by_run else None, "from_run_verified": verified, "fenced": fenced, "project": str(project) if project else None}
        try:
            path = cls.path()
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as log:
                log.write(json.dumps(line) + "\n")
        except OSError:
            pass
