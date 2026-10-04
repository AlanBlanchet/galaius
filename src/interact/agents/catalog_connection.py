"""Explicit service connection for the server-owned agent catalog.

Connection settings and private preview sessions persist separately from catalog
content. Standard authentication uses the existing protected token-file reader; a linked PC
(`auth_mode="machine"`) presents its own machine token, the one its channel already holds.
"""

import hashlib
import ipaddress
import json
import os
import typing
import warnings
from contextlib import ExitStack, contextmanager
from http.cookiejar import LWPCookieJar, LoadError
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from interact_core.accounts import Bootstrap
from pydantic import BaseModel, ConfigDict, model_validator

from interact import USER_AGENT
from interact.file_lock import exclusive
from interact.private_files import PRIVATE_FILES


class CatalogConnectionError(ValueError):
    """A catalog connection that cannot be used without changing its configuration."""


class CatalogAuthenticationError(CatalogConnectionError):
    """Authentication or workspace access was refused; cached access is forbidden."""


_PREVIEW_SIGN_IN = "/v1/auth/local-preview"
_UNLINKED = "this PC is no longer linked to its Interact server; run `interact login` to link it again"


class _Refused(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _session_payload(client: httpx.Client) -> str:
    jar = LWPCookieJar()
    for cookie in client.cookies.jar:
        jar.set_cookie(cookie)
    return "#LWP-Cookies-2.0\n" + jar.as_lwp_str(ignore_discard=True)


class CatalogConnection(BaseModel):
    """Validated connection settings, separate from replaceable catalog content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    workspace_id: UUID | None = None
    #: `machine`: this PC's own link (`interact.machines.MachineConfig`): its endpoint, company and
    #: token are read from the link on every load, so a re-linked PC is followed with no step. Never
    #: written to the connection file: the link is its only record (one writer, `MachineRunner`), so
    #: no install or sync rewrites a file other running interact processes read.
    auth_mode: Literal["preview", "token", "machine"]
    token_file: Path | None = None

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        try:
            parsed = urlsplit(self.endpoint)
            parsed.port
        except ValueError as error:
            raise ValueError("invalid catalog endpoint") from error
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        ):
            raise ValueError("catalog endpoint must be an HTTP(S) origin without credentials")
        loopback = parsed.hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if self.auth_mode == "preview" and not loopback:
            raise ValueError("preview authentication requires an explicit loopback endpoint")
        if not loopback and parsed.scheme != "https":
            raise ValueError("remote catalog authentication requires HTTPS")
        if (self.auth_mode == "token") != (self.token_file is not None):
            raise ValueError("token authentication requires a token file; no other mode accepts one")
        if self.token_file is not None and not self.token_file.is_absolute():
            raise ValueError("catalog token-file path must be absolute")
        return self

    @classmethod
    def path(cls) -> Path:
        # Circular layers: config.settings imports agents.providers for MEDIA_PROVIDERS;
        # providers imports catalog_connection for server-backed role discovery.
        from interact.config import UserConfig

        return UserConfig.PATH.parent / "agent-catalog-connection.json"

    @staticmethod
    def _link():
        """This PC's machine link, None when it is not linked."""
        # Circular layers: machines imports agents.run -> registry -> this module.
        from interact.machines import MachineRunner

        runner = MachineRunner()
        return runner.load() if runner.config_path.exists() else None

    @classmethod
    def linked(cls) -> Self | None:
        """The connection this PC's own link gives: its server, its company, its machine token."""
        link = cls._link()
        return None if link is None else cls(endpoint=link.server_url.rstrip("/"), workspace_id=link.workspace_id, auth_mode="machine")

    @classmethod
    def load(cls, path: Path | None = None) -> Self | None:
        target = path if path is not None else cls.path()
        try:
            payload = target.read_bytes()
        except FileNotFoundError:
            return cls.linked() if path is None else None
        if len(payload) > 16 * 1024:
            raise CatalogConnectionError("catalog connection exceeds its size limit")
        try:
            connection = cls.model_validate_json(payload)
        except ValueError as error:
            try:
                mode = json.loads(payload).get("auth_mode")
            except (ValueError, AttributeError):
                mode = None
            if isinstance(mode, str) and mode not in typing.get_args(cls.model_fields["auth_mode"].annotation):
                raise CatalogConnectionError(
                    f"catalog connection {target} names auth_mode {mode!r}, written by a newer interact; restart this process to load it"
                ) from error
            raise CatalogConnectionError("invalid catalog connection configuration") from error
        if connection.workspace_id is None:
            raise CatalogConnectionError("catalog connection has no selected workspace; sync again")
        # The loopback preview sign-in no longer exists on any server: a linked PC uses its own link.
        if connection.auth_mode in ("machine", "preview") and (linked := cls.linked()) is not None:
            return linked
        if connection.auth_mode == "machine":
            raise CatalogAuthenticationError(_UNLINKED)
        return connection

    @staticmethod
    def replace_text(path: Path, payload: str) -> None:
        """Atomically replace one local document, private to this user; a failed write keeps its predecessor."""
        PRIVATE_FILES.write_text(path, payload)

    def save(self, path: Path | None = None) -> None:
        if self.workspace_id is None:
            raise CatalogConnectionError("select a workspace before saving the catalog connection")
        target = path if path is not None else self.path()
        if self.auth_mode == "machine":
            target.unlink(missing_ok=True)  # the PC link is the connection: no second copy to clobber
            return
        self.replace_text(target, self.model_dump_json(indent=2))

    def connect(self, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
        headers = {"Origin": self.endpoint.rstrip("/"), "User-Agent": USER_AGENT}
        if self.token_file is not None:
            headers["Authorization"] = f"Bearer {PRIVATE_FILES.read_secret(self.token_file)}"
        elif self.auth_mode == "machine":
            link = self._link()
            if link is None or link.server_url.rstrip("/") != self.endpoint.rstrip("/") or link.workspace_id != self.workspace_id:
                raise CatalogAuthenticationError(_UNLINKED)
            headers["Authorization"] = f"Bearer {link.token.get_secret_value()}"
        return httpx.Client(
            base_url=self.endpoint.rstrip("/"), headers=headers,
            timeout=5, follow_redirects=False, trust_env=False, transport=transport,
        )

    def authenticate(self, client: httpx.Client) -> Self:
        if self.auth_mode == "preview":
            with self.session_lock():
                generation = self.access_generation()
                # Another process may have denied the cookies retained by this client.
                client.cookies.clear()
                jar = LWPCookieJar(self.session_path())
                try:
                    self.check_private_file(self.session_path())
                    # CookieJar's malformed-input warning includes the cookie line.
                    # Treat corruption as an empty session without exposing its contents.
                    with warnings.catch_warnings(record=True):
                        warnings.simplefilter("always")
                        jar.load(ignore_discard=True)
                except FileNotFoundError:
                    pass
                except LoadError:
                    jar.clear()
                    self.session_path().unlink(missing_ok=True)
                client.cookies.update(jar)
                client.cookies.jar.clear_expired_cookies()
                if not client.cookies:
                    self.request(client, "POST", _PREVIEW_SIGN_IN)
                resolved = self.resolve_workspace(client)
                payload = _session_payload(client)
                with self.access_guard(generation):
                    self.replace_text(self.session_path(), payload)
                    if resolved != self:
                        with resolved.access_guard():
                            self.replace_text(resolved.session_path(), payload)
                return resolved
        return self.resolve_workspace(client)

    def session_path(self) -> Path:
        identity = f"{self.endpoint.rstrip('/')}\n{self.auth_mode}\n{self.workspace_id}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        return self.path().parent / "agent-catalog-sessions" / f"{key}.cookies"

    @staticmethod
    def check_private_file(path: Path) -> None:
        try:
            PRIVATE_FILES.check(path)
        except PermissionError as error:
            raise CatalogConnectionError("catalog session file must be private and owned by the current user") from error
        if path.lstat().st_size > 64 * 1024:
            raise LoadError("catalog session exceeds its size limit")

    @contextmanager
    def session_lock(self, *, suffix: Literal[".lock", ".access-lock"] = ".lock"):
        path = self.session_path()
        if not path.parent.exists():
            PRIVATE_FILES.directory(path.parent)  # made private as it is created; an existing one is only checked
        try:
            PRIVATE_FILES.check(path.parent)
        except PermissionError as error:
            raise CatalogConnectionError("catalog session directory must be private and owned by the current user") from error
        with exclusive(os.open(path.with_suffix(suffix), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)):
            yield

    @contextmanager
    def access_guard(self, generation: UUID | None = None):
        """Serialize denial, publication and fallback across independent processes."""
        with self.session_lock(suffix=".access-lock"):
            path = self.session_path().with_suffix(".generation")
            try:
                self.check_private_file(path)
                current = UUID(path.read_text())
            except FileNotFoundError:
                current = UUID(int=0)
            except (ValueError, LoadError) as error:
                raise CatalogAuthenticationError("invalid catalog access generation; cached access is disabled") from error
            if generation is not None and current != generation:
                raise CatalogAuthenticationError("catalog access was invalidated during this operation; cached access is disabled")
            yield current

    def access_generation(self) -> UUID:
        with self.access_guard() as generation:
            return generation

    def invalidate_access(self, client: httpx.Client) -> None:
        # A bootstrap may still be preparing to publish this workspace's cookies.
        # Match bootstrap publication's lock order: unselected, then selected.
        connections = (self,) if self.workspace_id is None else (self.model_copy(update={"workspace_id": None}), self)
        with ExitStack() as locks:
            for connection in connections:
                locks.enter_context(connection.access_guard())
            for connection in connections:
                connection.replace_text(connection.session_path().with_suffix(".generation"), str(uuid4()))
                connection.session_path().unlink(missing_ok=True)
            client.cookies.clear()

    def resolve_workspace(self, client: httpx.Client) -> Self:
        # Token consumers can explicitly select their workspace. They need no cookie bootstrap.
        if self.workspace_id is not None:
            return self
        try:
            bootstrap = Bootstrap.model_validate_json(self.request(client, "GET", "/v1/bootstrap"))
        except ValueError as error:
            if isinstance(error, CatalogConnectionError):
                raise
            raise CatalogConnectionError("invalid catalog workspace bootstrap") from error
        return self.model_copy(update={"workspace_id": bootstrap.current_workspace_id})

    def request(self, client: httpx.Client, method: Literal["GET", "POST"], path: str) -> bytes:
        try:
            return self._request_once(client, method, path)
        except _Refused as refused:
            status = refused.status
            # A cached preview session can stop being honoured (expired, signed out elsewhere);
            # the loopback preview grants a fresh one, so ask once more before calling it a denial.
            if self.auth_mode == "preview" and path != _PREVIEW_SIGN_IN and self._sign_in_again(client):
                try:
                    return self._request_once(client, method, path)
                except _Refused as again:
                    status = again.status
            self.invalidate_access(client)
            raise CatalogAuthenticationError(
                f"catalog access refused (HTTP {status}); cached access is disabled"
                + (f"; {_UNLINKED}" if self.auth_mode == "machine" and status == 401 else "")
            ) from None

    def _sign_in_again(self, client: httpx.Client) -> bool:
        client.cookies.clear()
        try:
            self._request_once(client, "POST", _PREVIEW_SIGN_IN)
        except (_Refused, CatalogConnectionError):
            return False
        with self.access_guard():
            self.replace_text(self.session_path(), _session_payload(client))
        return True

    def _request_once(self, client: httpx.Client, method: Literal["GET", "POST"], path: str) -> bytes:
        with client.stream(method, path) as response:
            workspace_refused = response.status_code == 404 and path.startswith("/v1/workspaces/")
            if response.status_code in {401, 403} or workspace_refused:
                raise _Refused(response.status_code)
            if response.status_code < 200 or response.status_code >= 300:
                raise CatalogConnectionError(f"catalog request failed (HTTP {response.status_code})")
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 16 * 1024 * 1024:
                    raise CatalogConnectionError("catalog response exceeds its size limit")
            return bytes(payload)
