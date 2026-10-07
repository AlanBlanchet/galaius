import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from galaius.prompt_publisher import publish_projection
from galaius.prompt_projection import MANIFEST_NAME
from galaius_core import (
    PromptCatalogPage,
    PromptChannelEntry,
    PromptKey,
    PromptPublicationRequest,
)


def test_publication_request_requires_one_commit_and_unique_keys() -> None:
    fields = PromptPublicationRequest.model_fields
    assert set(fields) == {"expected_cursor", "source_commit", "entries", "revisions"}


def test_publisher_sends_complete_snapshot_and_only_changed_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = tmp_path / "projection"
    projection.mkdir()
    contents = {"AGENTS.md": "same bytes\n", "instructions.md": "changed\n"}
    outputs = []
    for path, content in contents.items():
        target = projection / path
        target.write_text(content)
        target.chmod(0o644)
        outputs.append({
            "path": path, "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "size": len(content.encode()), "mode": "0644", "consumers": ["test"],
        })
    (projection / MANIFEST_NAME).write_text(json.dumps({
        "version": 1, "source_commit": "b" * 40,
        "source_timestamp": "2026-09-05T12:00:00+00:00", "outputs": outputs,
    }))
    same_key = PromptKey(
        namespace="galaius-projection",
        slug="output-" + hashlib.sha256(b"AGENTS.md").hexdigest()[:24],
    )
    changed_key = PromptKey(
        namespace="galaius-projection",
        slug="output-" + hashlib.sha256(b"instructions.md").hexdigest()[:24],
    )
    removed_key = PromptKey(namespace="galaius-projection", slug="removed")
    prior_digest = "d" * 64
    catalog = PromptCatalogPage(
        entries=(
            PromptChannelEntry(
                key=same_key, channel="stable", revision=UUID(int=1),
                digest=outputs[0]["sha256"], lock_version=0,
            ),
            PromptChannelEntry(
                key=changed_key, channel="stable", revision=UUID(int=2),
                digest=prior_digest, lock_version=0,
            ),
            PromptChannelEntry(
                key=removed_key, channel="stable", revision=UUID(int=3),
                digest="f" * 64, lock_version=0,
            ),
        ),
        cursor="e" * 64, server_timestamp="2026-09-05T12:00:00Z",
    )
    requests: list[PromptPublicationRequest] = []

    def request(
        endpoint: str, token: str, method: str, path: str, body: object | None = None,
        max_bytes: int = 1024 * 1024,
    ) -> object:
        del endpoint, token, max_bytes
        if method == "GET":
            return catalog.model_dump(mode="json")
        assert path == "/v1/publications"
        requests.append(PromptPublicationRequest.model_validate(body))
        return catalog.model_dump(mode="json")

    monkeypatch.setattr("galaius.prompt_publisher._request", request)
    publish_projection(projection, "http://localhost", "secret")

    publication = requests.pop()
    assert len(publication.entries) == 2
    assert len(publication.revisions) == 1
    assert publication.revisions[0].content == "changed\n"
    assert publication.revisions[0].parent_digest == prior_digest
    assert removed_key not in {entry.key for entry in publication.entries}


def test_identical_content_at_distinct_paths_keeps_distinct_prompt_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = tmp_path / "projection"
    projection.mkdir()
    content = "identical\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    outputs = []
    for path in ("AGENTS.md", "instructions.md"):
        (projection / path).write_text(content)
        (projection / path).chmod(0o644)
        outputs.append({
            "path": path, "sha256": digest, "size": len(content.encode()),
            "mode": "0644", "consumers": ["test"],
        })
    (projection / MANIFEST_NAME).write_text(json.dumps({
        "version": 1, "source_commit": "c" * 40,
        "source_timestamp": "2026-09-05T12:00:00+00:00", "outputs": outputs,
    }))
    empty = PromptCatalogPage(
        entries=(), cursor=None, server_timestamp="2026-09-05T12:00:00Z",
    )
    captured: list[PromptPublicationRequest] = []

    def request(
        endpoint: str, token: str, method: str, path: str, body: object | None = None,
        max_bytes: int = 1024 * 1024,
    ) -> object:
        del endpoint, token, path, max_bytes
        if method == "POST":
            captured.append(PromptPublicationRequest.model_validate(body))
        return empty.model_dump(mode="json")

    monkeypatch.setattr("galaius.prompt_publisher._request", request)
    publish_projection(projection, "http://localhost", "secret")
    publication = captured.pop()
    assert len({(entry.key.namespace, entry.key.slug) for entry in publication.entries}) == 2
    assert len(publication.revisions) == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_cursor", "not-a-cursor", "cursor"),
        ("source_commit", "not-a-commit", "source commit"),
        ("source_commit", "A" * 40, "source commit"),
    ],
)
def test_publication_request_rejects_unbound_repository_identifiers(
    field: str, value: str, message: str,
) -> None:
    values: dict[str, object] = {
        "expected_cursor": None, "source_commit": "a" * 40, "entries": (), "revisions": (),
    }
    values[field] = value
    with pytest.raises(ValueError, match=message):
        PromptPublicationRequest.model_validate(values)
