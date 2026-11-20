"""The machine file: where it is, who may write it, and the enrollment a connection must still match."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from galaius.host.file_lock import exclusive
from galaius.host.paths import UserPaths
from galaius.host.private_files import PRIVATE_FILES
from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.faults import EnrollmentChanged
from galaius.machine.connection.logs import audit
from galaius.machine.connection.shared import Shared


class MachineFile(Shared):
    """Read, save and update this machine's file (`config_path`) under one lock."""

    #: Appends one line to an owner-only log beside it (`logs.audit`): log name, entry.
    audit = staticmethod(audit)

    @staticmethod
    def default_config_path() -> Path:
        return UserPaths.config() / "machine.json"

    def _config_lock(self):
        """Held by every writer of the machine file (`machine.lock` beside it, never replaced)."""
        PRIVATE_FILES.directory(self.config_path.parent)
        return exclusive(os.open(self.config_path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))

    def save(self, config: MachineConfig) -> None:
        with self._config_lock():
            self._save(config)

    def update(self, change: Callable[[MachineConfig], MachineConfig]) -> MachineConfig:
        """Read, merge and atomically replace under the same lock as enrollment and owner edits. A
        change of the agent settings made here counts one more `settings_revision`."""
        with self._config_lock():
            before = self.load()
            config = MachineConfig.model_validate(change(before).model_dump())
            if config.settings_revision == before.settings_revision and config.agent_settings()[0] != before.agent_settings()[0]:
                config = config.model_copy(update={"settings_revision": before.settings_revision + 1})
            self._save(config)
            return config

    def _save(self, config: MachineConfig) -> None:
        """The token sealed as `PRIVATE_FILES` stores secrets (as is on POSIX, DPAPI on Windows)."""
        values = config.model_dump(mode="json")
        values["token"] = PRIVATE_FILES.seal(config.token.get_secret_value())
        PRIVATE_FILES.write_text(self.config_path, json.dumps(values, separators=(",", ":")) + "\n")

    def load(self) -> MachineConfig:
        return self.read(self.config_path)

    @staticmethod
    def read(path: Path) -> MachineConfig:
        """The machine file at `path`, with no runner built around it."""
        values = json.loads(PRIVATE_FILES.read_text(path))
        values["token"] = PRIVATE_FILES.unseal(values["token"])
        # Machine files written before levels: each file root was read + write apart from every
        # other root, i.e. a sandbox (kept as it was; the next save drops `file_roots`).
        for name in values.pop("file_roots", ()):
            values.setdefault("places", {}).setdefault(name, "sandbox")
        # Machine files written while a web widening waited for a confirm here: the web's level
        # now applies at once, so a widening still waiting is dropped (the web asks it again).
        values.pop("pending_places", None)
        return MachineConfig.model_validate(values)

    def _current_config(self, connected: MachineConfig) -> MachineConfig:
        """Current owner permissions, or refusal. A stale connection can never restore access."""
        current = self.load()
        self._same_enrollment(current, connected)
        return current

    @staticmethod
    def _same_enrollment(current: MachineConfig, connected: MachineConfig) -> None:
        if (current.machine_id, current.workspace_id, current.server_url, current.token) != (connected.machine_id, connected.workspace_id, connected.server_url, connected.token):
            raise EnrollmentChanged("machine enrollment changed on this machine; reconnecting with it")
