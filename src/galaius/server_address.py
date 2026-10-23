"""One Galaius server's address, as THIS computer reaches it.

Owned here because two callers need the same rules: `galaius login` (which server to sign in to,
`AccountLogin`) and this computer's machine link (which address to keep its channel open on,
`MachineRunner`). The rule both follow: an address only this computer answers at — an SSH tunnel's
`127.0.0.1`, a server running here — is never what a computer is enrolled with; the public address
that server names replaces it (`public`), and while no server can be asked, the one this computer
remembers does (`remembered`)."""

from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from galaius import USER_AGENT
from galaius.paths import UserPaths


class ServerAddressError(ValueError):
    """An address that is no Galaius server address."""


class ServerAddress(BaseModel):
    """`server`: the scheme and host every call to that server goes to."""

    model_config = ConfigDict(frozen=True)
    server: str
    #: Host names only this computer answers at: an SSH tunnel's end, a server running here.
    loopback: ClassVar[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

    @classmethod
    def parsed(cls, server: str):
        """`server` as an address, a bare host meaning https; http is for a loopback host only."""
        server = server.strip()
        if "://" not in server:
            server = f"https://{server}"
        parts = urlsplit(server)
        local = parts.hostname in cls.loopback
        if parts.scheme not in ({"https", "http"} if local else {"https"}) or not parts.hostname or parts.username or parts.path not in {"", "/"}:
            raise ServerAddressError(f"{server} is not a Galaius server address (https://…)")
        return cls(server=f"{parts.scheme}://{parts.netloc}")

    @staticmethod
    def remembered_path() -> Path:
        """Written by the server's install script."""
        return UserPaths.config() / "login-server"

    @classmethod
    def remembered(cls):
        """The address this computer remembers (the installer's or an earlier login's), as written:
        asked of nothing, so it still answers while no server can be reached at all."""
        try:
            return cls.parsed(cls.remembered_path().read_text(encoding="utf-8"))
        except (OSError, ServerAddressError):
            return None

    @property
    def local(self) -> bool:
        """Whether this address is one only this computer answers at (`loopback`)."""
        return urlsplit(self.server).hostname in self.loopback

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.server, headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(15, connect=10), trust_env=False, follow_redirects=False)

    def answers(self) -> bool:
        try:
            with self.client() as http:
                return http.get("/v1/version").status_code == 200
        except httpx.HTTPError:
            return False

    def public(self):
        """This address as another computer reaches it: a loopback address is swapped for the public
        address its own server names, and dropped when it does not answer; a public address is kept
        as is. Asks that server, so None also means it could not be asked at all."""
        if not self.local:
            return self
        try:
            with self.client() as http:
                named = http.get("/v1/install").json().get("server")
                public = None if not named else self.parsed(named)
                if public is not None and not public.local and public.answers():
                    return public
                return self if http.get("/v1/version").status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
