"""Every interact command runs the ACTIVE runtime's code, whatever program started it.

An installer puts one `interact` on the computer (uv's tool install) and never touches it again;
upgrades land in the runtime store. So the command line is handed to the active runtime before
anything else runs: POSIX replaces this process (`execv`, same pid: a service manager or terminal
sees nothing), Windows runs it as a child and waits, passing its exit code on, and starts it again
when a supervisor in it asks to become a newer runtime's (`EXIT_HANDOVER`). `interact upgrade` never
hands off: it is how a person repairs a broken active runtime."""

import os
import subprocess
import sys
from typing import ClassVar, NoReturn

from pydantic import BaseModel, ConfigDict

from interact.upgrade.store import EXIT_HANDOVER, Runtime, RuntimeStore


class Handoff(BaseModel):
    model_config = ConfigDict(frozen=True)
    store: RuntimeStore
    #: Set in the child a Windows parent runs: it already is the active runtime's.
    child: ClassVar[str] = "INTERACT_HANDED_OFF"
    #: Commands that always run the code they were started with.
    own_code: ClassVar[frozenset[str]] = frozenset({"upgrade"})

    def target(self, command: tuple[str, ...], long_lived: bool) -> Runtime | None:
        """Where this command line continues, or None to run it here. A supervisor handover that
        never started is rolled back first (said on stderr)."""
        if (command[:1] and command[0] in self.own_code) or os.environ.get(self.child):
            return None
        if (back := self.store.failed_handover()) is not None:
            print(f"interact: the newest runtime never started as a supervisor; back on {back.label()}", file=sys.stderr)
        active = self.store.active()
        if active.path != Runtime.own().path:
            return active
        return active if long_lived and sys.platform == "win32" else None

    def continue_in(self, runtime: Runtime, command: tuple[str, ...]) -> NoReturn:
        sys.stdout.flush()
        sys.stderr.flush()
        if sys.platform != "win32":
            os.execv(runtime.python, runtime.command(command))
        environment = {**os.environ, self.child: "1"}
        while True:
            code = subprocess.call(runtime.command(command), env=environment)
            if code != EXIT_HANDOVER:
                raise SystemExit(code)
            runtime = self.store.active()

    def replace(self, runtime: Runtime, command: tuple[str, ...]) -> NoReturn:
        """A running supervisor becomes `runtime`'s: in place on POSIX, through its waiting parent
        on Windows. Remembered until the new one starts (`RuntimeStore.failed_handover`)."""
        self.store.hand_over(runtime)
        sys.stdout.flush()
        sys.stderr.flush()
        if sys.platform != "win32":
            os.execv(runtime.python, runtime.command(command))
        raise SystemExit(EXIT_HANDOVER)
