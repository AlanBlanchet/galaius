"""Current workspace prompt heads through the configured catalog connection."""

import hashlib
import re
from contextlib import contextmanager

import httpx
from interact_core.accounts import Bootstrap
from interact_core.prompts import PromptCreateRequest, PromptKey, PromptRevision, PromptSyncStatus
from pydantic import BaseModel, ConfigDict, TypeAdapter

from interact.agents.catalog_connection import (
    CatalogAuthenticationError,
    CatalogConnection,
    CatalogConnectionError,
)

MAX_EDITOR_BYTES = 1 << 20
_MAX_RESPONSE_BYTES = 16 << 20
_MAX_CATALOG_ENTRIES = 4096


class PromptConflictError(CatalogConnectionError):
    """The key already exists or the editor's base revision is no longer the server head."""


class ServerPrompts(BaseModel):
    """Prompt-specific GET/POST/PUT adapter; never reads or writes local prompt sources."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    connection: CatalogConnection

    @staticmethod
    def key(path: str) -> PromptKey:
        if len(path) > 256 or not path.endswith(".md") or path.count("/") != 1:
            raise ValueError("prompt path must be namespace/slug.md (at most 256 characters)")
        namespace, slug = path[:-3].split("/")
        try:
            return PromptKey(namespace=namespace, slug=slug)
        except ValueError as error:
            raise ValueError("prompt path must use lowercase kebab-case namespace/slug.md") from error

    @staticmethod
    def path(key: PromptKey) -> str:
        path = f"{key.namespace}/{key.slug}.md"
        ServerPrompts.key(path)
        return path

    @staticmethod
    def validate_revision(payload: bytes, key: PromptKey) -> PromptRevision:
        try:
            revision = PromptRevision.model_validate_json(payload)
        except ValueError as error:
            raise CatalogConnectionError("invalid server prompt revision") from error
        if revision.key != key:
            raise CatalogConnectionError("server returned a different prompt key")
        if len(revision.content.encode("utf-8")) > MAX_EDITOR_BYTES:
            raise CatalogConnectionError("server prompt exceeds the editor size limit")
        return revision

    @contextmanager
    def session(self, *, transport: httpx.BaseTransport | None = None):
        generation = self.connection.access_generation()
        with self.connection.connect(transport=transport) as client:
            connection = self.connection.authenticate(client)
            if connection != self.connection:
                raise CatalogConnectionError("prompt connection requires a selected workspace")
            with connection.access_guard(generation):
                pass
            yield client
            with connection.access_guard(generation):
                pass

    @property
    def endpoint(self) -> str:
        if self.connection.workspace_id is None:
            raise CatalogConnectionError("prompt connection requires a selected workspace")
        return f"/v1/workspaces/{self.connection.workspace_id}/prompts"

    def catalog(self, *, transport: httpx.BaseTransport | None = None) -> tuple[PromptRevision, ...]:
        with self.session(transport=transport) as client:
            payload = self.connection.request(client, "GET", self.endpoint)
            try:
                revisions = TypeAdapter(tuple[PromptRevision, ...]).validate_json(payload)
            except ValueError as error:
                raise CatalogConnectionError("invalid server prompt catalog") from error
            if len(revisions) > _MAX_CATALOG_ENTRIES:
                raise CatalogConnectionError("server prompt catalog exceeds its entry limit")
            paths = [self.path(item.key) for item in revisions]
            if len(set(paths)) != len(paths):
                raise CatalogConnectionError("server prompt catalog contains duplicate keys")
            if any(len(item.content.encode("utf-8")) > MAX_EDITOR_BYTES for item in revisions):
                raise CatalogConnectionError("server prompt exceeds the editor size limit")
            return tuple(sorted(revisions, key=lambda item: self.path(item.key)))

    def read(self, path: str, *, transport: httpx.BaseTransport | None = None) -> PromptRevision:
        key = self.key(path)
        with self.session(transport=transport) as client:
            return self.read_head(client, key)

    def read_head(self, client: httpx.Client, key: PromptKey) -> PromptRevision:
        payload = self.connection.request(client, "GET", f"{self.endpoint}/{key.namespace}/{key.slug}")
        return self.validate_revision(payload, key)

    def create(
        self, path: str, content: str, *, name: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> PromptRevision:
        key = self.key(path)
        self.require_write(content)
        proposed = PromptCreateRequest(key=key, name=key.slug if name is None else name, content=content)
        with self.session(transport=transport) as client:
            return self.submit_revision(client, proposed)

    def require_write(self, content: str) -> None:
        if len(content.encode("utf-8")) > MAX_EDITOR_BYTES:
            raise ValueError("prompt source is too large")
        if self.connection.auth_mode == "token":
            raise CatalogConnectionError("token authentication is read-only; save through the server's signed-in prompt editor")

    def write(
        self, path: str, digest: str, content: str, *, transport: httpx.BaseTransport | None = None,
    ) -> PromptRevision:
        key = self.key(path)
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("editor digest must be lowercase SHA-256")
        self.require_write(content)
        with self.session(transport=transport) as client:
            current = self.read_head(client, key)
            if current.digest != digest:
                raise PromptConflictError("server prompt changed; preserve the editor buffer and reload")
            if current.content == content:
                return current
            proposed = current.model_copy(update={
                "content": content, "digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "parent_digest": digest,
            })
            return self.submit_revision(client, proposed)

    def submit_revision(self, client: httpx.Client, proposed: PromptCreateRequest | PromptRevision) -> PromptRevision:
        """One bounded authenticated mutation path for create and compare-and-swap updates."""
        self.require_write(proposed.content)
        try:
            bootstrap = Bootstrap.model_validate_json(self.connection.request(client, "GET", "/v1/bootstrap"))
        except ValueError as error:
            if isinstance(error, CatalogConnectionError):
                raise
            raise CatalogConnectionError("invalid server session bootstrap") from error
        payload = proposed.model_dump_json().encode("utf-8")
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ValueError("prompt request exceeds its size limit")
        creating = isinstance(proposed, PromptCreateRequest)
        key = proposed.key
        endpoint = self.endpoint if creating else f"{self.endpoint}/{key.namespace}/{key.slug}"
        with client.stream(
            "POST" if creating else "PUT", endpoint, content=payload,
            headers={"Content-Type": "application/json", "x-csrf-token": bootstrap.csrf_token},
        ) as response:
            if response.status_code in {401, 403}:
                self.connection.invalidate_access(client)
                raise CatalogAuthenticationError(f"prompt save refused (HTTP {response.status_code}); preserve the editor buffer")
            if response.status_code == 409:
                raise PromptConflictError("server prompt already exists or changed; preserve the editor buffer and reload")
            if not 200 <= response.status_code < 300:
                raise CatalogConnectionError(f"prompt save failed (HTTP {response.status_code}); preserve the editor buffer")
            result = bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result) > _MAX_RESPONSE_BYTES:
                    raise CatalogConnectionError("server prompt response exceeds its size limit")
        saved = self.validate_revision(bytes(result), key)
        if saved.content != proposed.content or saved.name != proposed.name:
            raise CatalogConnectionError("server save response does not match submitted revision; preserve the editor buffer and reload")
        return saved

    def status(self, *, transport: httpx.BaseTransport | None = None) -> PromptSyncStatus:
        with self.session(transport=transport) as client:
            payload = self.connection.request(client, "GET", f"{self.endpoint}/sync-status")
            try:
                return PromptSyncStatus.model_validate_json(payload)
            except ValueError as error:
                raise CatalogConnectionError("invalid server prompt sync status") from error
