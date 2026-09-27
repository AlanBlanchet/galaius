"""Personal portable preferences through the existing authenticated catalog session."""

import hashlib
import json
from datetime import UTC, datetime
from http.cookiejar import LWPCookieJar
from uuid import UUID

import httpx
from interact_core import PortableToolSettings, PortableToolSettingsUpdate, PortableToolSettingsValues
from interact_core.accounts import Bootstrap
from pydantic import BaseModel, ConfigDict, ValidationError

from interact.agents.catalog_connection import CatalogAuthenticationError, CatalogConnection, CatalogConnectionError
from interact.config.settings import Config
from interact.server_prompts import ServerPrompts

PORTABLE_ENV = {f"INTERACT_{name.upper()}": name for name in PortableToolSettingsValues.model_fields}
_LIMIT = 256 * 1024


def validate_values(values: PortableToolSettingsValues) -> None:
    defaults = {name: field.get_default(call_default_factory=True) for name, field in Config.model_fields.items()}
    try:
        Config(**(defaults | values.model_dump(exclude_none=True)))
    except ValidationError as error:
        reasons = "; ".join(issue["msg"] for issue in error.errors(include_input=False, include_context=False))
        raise CatalogConnectionError(f"Personal settings incompatible with client defaults: {reasons}") from error


def environment_values(values: PortableToolSettingsValues) -> dict[str, str]:
    return {env: ",".join(value) if isinstance(value, tuple) else str(value)
            for env, field in PORTABLE_ENV.items() if (value := getattr(values, field)) is not None}


class ToolSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID
    generation: UUID
    session_digest: str
    expires_at: datetime
    settings: PortableToolSettings
    stale: bool = False

    @property
    def env(self) -> dict[str, str]:
        return environment_values(self.settings.values)

    def status(self) -> dict:
        return {"configured": True, "source": "server", "account_id": str(self.account_id),
                "revision": self.settings.revision, "stale": self.stale, "values": self.env,
                "portable_keys": list(PORTABLE_ENV)}


class ToolSettingsConflict(CatalogConnectionError):
    """The editor retains its base and proposed values until the user reloads."""


class ServerToolSettings(ServerPrompts):
    @classmethod
    def configured(cls):
        connection = CatalogConnection.load()
        return cls(connection=connection) if connection else None

    @property
    def cache_path(self):
        return self.connection.session_path().with_suffix(".tool-settings.json")

    def session_digest(self, client: httpx.Client | None = None) -> str:
        """Bind cached identity to the actual session without storing its credential."""
        if client is None:
            path = self.connection.session_path()
            self.connection.check_private_file(path)
            jar = LWPCookieJar(path)
            jar.load(ignore_discard=True)
        else:
            jar = client.cookies.jar
        cookies = sorted((cookie.domain, cookie.path, cookie.name, cookie.value, cookie.expires) for cookie in jar)
        return hashlib.sha256(json.dumps(cookies).encode()).hexdigest()

    def require_actor(self) -> None:
        if self.connection.auth_mode == "token":
            self.cache_path.unlink(missing_ok=True)
            raise CatalogAuthenticationError("Personal settings require a signed-in account; workspace tokens cannot read or change them.")

    def bootstrap(self, client: httpx.Client) -> Bootstrap:
        bootstrap = Bootstrap.model_validate_json(self.connection.request(client, "GET", "/v1/bootstrap"))
        if bootstrap.session_expires_at.tzinfo is None or bootstrap.session_expires_at <= datetime.now(UTC):
            raise ValueError("expired or invalid personal session")
        return bootstrap

    def decode(self, payload: bytes) -> PortableToolSettings:
        if len(payload) > _LIMIT:
            raise CatalogConnectionError("tool settings response exceeds its limit")
        settings = PortableToolSettings.model_validate_json(payload)
        validate_values(settings.values)
        return settings

    def publish(self, settings: PortableToolSettings, bootstrap: Bootstrap, generation: UUID, session_digest: str) -> ToolSettingsSnapshot:
        snapshot = ToolSettingsSnapshot(account_id=bootstrap.account.account_id, generation=generation,
            session_digest=session_digest, expires_at=bootstrap.session_expires_at, settings=settings)
        with self.connection.session_lock(), self.connection.access_guard(generation):
            if self.session_digest() != session_digest:
                self.cache_path.unlink(missing_ok=True)
                raise CatalogAuthenticationError("Personal session changed during request; reload settings.")
            try:
                self.connection.check_private_file(self.cache_path)
                cached = ToolSettingsSnapshot.model_validate_json(self.cache_path.read_bytes())
            except (OSError, ValueError):
                cached = None
            if (cached is not None and cached.account_id == snapshot.account_id
                    and cached.generation == generation and cached.session_digest == session_digest
                    and cached.settings.revision > settings.revision):
                return cached
            self.connection.replace_text(self.cache_path, snapshot.model_dump_json())
        return snapshot

    def read(self, *, allow_stale: bool = True) -> ToolSettingsSnapshot:
        self.require_actor()
        generation = self.connection.access_generation()
        account_id = None
        try:
            with self.session() as client:
                bootstrap = self.bootstrap(client)
                account_id = bootstrap.account.account_id
                settings = self.decode(self.connection.request(client, "GET", "/v1/account/tool-settings"))
                session_digest = self.session_digest(client)
            return self.publish(settings, bootstrap, generation, session_digest)
        except httpx.TransportError as error:
            if allow_stale:
                try:
                    with self.connection.session_lock(), self.connection.access_guard(generation):
                        self.connection.check_private_file(self.cache_path)
                        snapshot = ToolSettingsSnapshot.model_validate_json(self.cache_path.read_bytes())
                        validate_values(snapshot.settings.values)
                        if (snapshot.generation == generation and snapshot.session_digest == self.session_digest()
                                and snapshot.expires_at.tzinfo is not None and snapshot.expires_at > datetime.now(UTC)
                                and (account_id is None or account_id == snapshot.account_id)):
                            return snapshot.model_copy(update={"stale": True})
                except (OSError, ValueError):
                    pass
            raise CatalogConnectionError("Personal settings unavailable; no verified account cache. Local portable settings are not a fallback.") from error
        except (ValueError, OSError) as error:
            self.cache_path.unlink(missing_ok=True)
            if isinstance(error, CatalogConnectionError):
                raise
            raise CatalogConnectionError("Invalid personal settings response; cached settings disabled.") from error

    def update(self, changes: dict[str, str | None], *, base: ToolSettingsSnapshot) -> ToolSettingsSnapshot:
        self.require_actor()
        if base.stale:
            raise ToolSettingsConflict("Cached settings are stale. Refresh online before saving; draft retained.")
        values = base.settings.values.model_dump(exclude_none=True)
        for env, value in changes.items():
            if env not in PORTABLE_ENV:
                raise ValueError("Only portable setting names may be sent to the server")
            field = PORTABLE_ENV[env]
            if value is None:
                values.pop(field, None)
            else:
                values[field] = tuple(value.split(",")) if field == "media_provider_order" else value
        proposed = PortableToolSettingsValues.model_validate(values)
        validate_values(proposed)
        update = PortableToolSettingsUpdate(expected_revision=base.settings.revision, values=proposed)
        try:
            with self.connection.access_guard(base.generation):
                pass
            with self.session() as client:
                bootstrap = self.bootstrap(client)
                if bootstrap.account.account_id != base.account_id:
                    self.cache_path.unlink(missing_ok=True)
                    raise ToolSettingsConflict("Signed-in account changed. Reload before saving; draft retained.")
                with client.stream("PUT", "/v1/account/tool-settings", content=update.model_dump_json(exclude_none=True),
                        headers={"Content-Type": "application/json", "x-csrf-token": bootstrap.csrf_token}) as response:
                    if response.status_code in {401, 403}:
                        self.connection.invalidate_access(client)
                        self.cache_path.unlink(missing_ok=True)
                        raise CatalogAuthenticationError("Personal settings save refused; sign in again. Draft retained.")
                    if response.status_code == 409:
                        raise ToolSettingsConflict("Personal settings changed on another client. Reload before saving; draft retained.")
                    if response.status_code != 200:
                        raise CatalogConnectionError(f"Personal settings save failed (HTTP {response.status_code}); draft retained.")
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > _LIMIT:
                            raise ValueError("oversized settings")
                saved = self.decode(bytes(payload))
                if saved.values != proposed or saved.revision <= base.settings.revision:
                    raise ValueError("mismatched saved settings")
                session_digest = self.session_digest(client)
            return self.publish(saved, bootstrap, base.generation, session_digest)
        except httpx.TransportError as error:
            raise CatalogConnectionError("Save response unavailable; draft retained. Refresh before retrying.") from error
        except ValueError as error:
            if isinstance(error, CatalogConnectionError):
                raise
            self.cache_path.unlink(missing_ok=True)
            raise CatalogConnectionError("Invalid settings save response; draft retained and cache disabled.") from error
