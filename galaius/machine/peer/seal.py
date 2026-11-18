"""What this runner tells its server about itself at every hello: a random id of this process and the seal of its last accepted hello.

The seal is kept like the machine token (a private file beside it) and replaced each time the server answers a hello with a new one; the server
reads a connection that presents neither the newest nor the one before as a copy of this PC's files (`PeerHello`). The instance lives in memory only."""

import logging
from pathlib import Path
from uuid import UUID, uuid4

from galaius_core import PeerHello, PeerSealed

from galaius.host.private_files import PRIVATE_FILES
from galaius.machine.peer.switch import PeerSwitch

logger = logging.getLogger(__name__)
#: Made once per process: a reconnect of this process presents the same one, a second process presents another.
INSTANCE: UUID = uuid4()


class PeerSeal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def held(self) -> str | None:
        try:
            return PRIVATE_FILES.read_secret(self.path)
        except (OSError, ValueError):
            return None

    def hello(self) -> dict[str, object]:
        """The `peer` entry of a hello."""
        return PeerHello(instance=INSTANCE, seal=self.held(), off=PeerSwitch.is_off()).model_dump(mode="json", exclude_none=True)

    def keep(self, said: object) -> bool:
        """The server's `peer_sealed` message, stored; False when it is malformed (the old seal stays: a lost write is forgiven once by the server)."""
        try:
            sealed = PeerSealed.model_validate(said)
        except ValueError:
            return False
        try:
            PRIVATE_FILES.write_secret(self.path, sealed.seal)
        except OSError:
            logger.warning("the seal the server gave could not be stored: the next hello presents the one before (forgiven once)", exc_info=True)
            return False
        return True
