"""Explicit atomic publication of one exact compiled prompt projection."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
import urllib.request
from uuid import NAMESPACE_URL, uuid5

from interact_core import (
    PromptCatalogPage,
    PromptKey,
    PromptPublicationRequest,
    PromptRevision,
    PromptSelection,
)

from interact import USER_AGENT
from interact.prompt_projection import MANIFEST_NAME, _validated_manifest_outputs


def publish_projection(projection: Path, endpoint: str, token: str) -> PromptCatalogPage:
    """Send one complete snapshot; the server applies or rejects it as one transaction."""
    outputs = _validated_manifest_outputs(projection)
    manifest = json.loads((projection / MANIFEST_NAME).read_text(encoding="utf-8"))
    catalog = PromptCatalogPage.model_validate(_request(endpoint, token, "GET", "/v1/catalog"))
    timestamp = datetime.fromisoformat(manifest["source_timestamp"])
    candidates = tuple(sorted((
        PromptRevision(
            key=PromptKey(
                namespace="interact-projection",
                slug="output-" + hashlib.sha256(relative.encode()).hexdigest()[:24],
            ),
            revision=uuid5(NAMESPACE_URL, f"{manifest['source_commit']}:{relative}"),
            digest=hashlib.sha256(source.read_bytes()).hexdigest(),
            content=source.read_text(encoding="utf-8"),
            source_commit=manifest["source_commit"],
            created_at=timestamp,
        )
        for relative, source in sorted(outputs.items())
    ), key=lambda revision: (revision.key.namespace, revision.key.slug)))
    entries = tuple(
        PromptSelection(key=revision.key, channel="stable", digest=revision.digest)
        for revision in candidates
    )
    existing = {
        (entry.key.namespace, entry.key.slug): entry.digest
        for entry in catalog.entries if entry.channel == "stable"
    }
    requested = {
        (entry.key.namespace, entry.key.slug): entry.digest for entry in entries
    }
    if existing == requested:
        return catalog
    revisions = tuple(
        revision.model_copy(update={"parent_digest": existing.get(
            (revision.key.namespace, revision.key.slug)
        )})
        for revision in candidates
        if existing.get((revision.key.namespace, revision.key.slug)) != revision.digest
    )
    publication = PromptPublicationRequest(
        expected_cursor=catalog.cursor,
        source_commit=manifest["source_commit"],
        entries=entries,
        revisions=revisions,
    )
    return PromptCatalogPage.model_validate(_request(
        endpoint, token, "POST", "/v1/publications",
        publication.model_dump(mode="json"), max_bytes=16 * 1024 * 1024,
    ))


def _request(
    endpoint: str, token: str, method: str, path: str, body: object | None = None,
    max_bytes: int = 1024 * 1024,
) -> object:
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    if encoded is not None and len(encoded) > 16 * 1024 * 1024:
        raise ValueError("prompt publication exceeds the request size limit")
    request = urllib.request.Request(
        endpoint.rstrip("/") + path, data=encoded, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("prompt publication response exceeds the size limit")
        return json.loads(payload)
