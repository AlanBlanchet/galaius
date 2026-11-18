"""The owner's switch for the runs other of his PCs place on this one: OFF can be set anywhere, ON only by the server (his web session).

A file beside the machine file: present = off. The local command `galaius machine peer off` writes it; the server's `peer_switch` message (sent when
his web session turns peer runs on) removes it. No local command turns it on, so a run placed here cannot switch itself back on."""

from galaius.host.paths import UserPaths


class PeerSwitch:
    @staticmethod
    def path():
        return UserPaths.config() / "peer-off"

    @classmethod
    def is_off(cls) -> bool:
        return cls.path().exists()

    @classmethod
    def off(cls) -> None:
        cls.path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls.path().touch(mode=0o600)

    @classmethod
    def on(cls, said_by_server: bool) -> None:
        """Only the server's message passes True."""
        if said_by_server:
            cls.path().unlink(missing_ok=True)
