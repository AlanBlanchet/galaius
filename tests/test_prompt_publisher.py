import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
from uuid import UUID

import pytest

from interact.prompt_publisher import publish_projection
from interact.prompt_projection import MANIFEST_NAME
from interact.prompt_secret import read_prompt_token
from interact_core import (
    PromptCatalogPage,
    PromptChannelEntry,
    PromptKey,
    PromptPublicationRequest,
)


@pytest.mark.parametrize("case", ["mode", "symlink", "oversize"])
def test_publication_token_file_is_bounded_private_and_regular(tmp_path: Path, case: str) -> None:
    token = tmp_path / "token"
    token.write_text("test-token\n" if case != "oversize" else "x" * 4097)
    token.chmod(0o600 if case != "mode" else 0o644)
    candidate = token
    if case == "symlink":
        candidate = tmp_path / "linked"
        candidate.symlink_to(token)
    with pytest.raises(ValueError, match="token file"):
        read_prompt_token(candidate)


def test_real_prompt_service_publishes_exact_projection_idempotently(tmp_path: Path) -> None:
    server = Path(__file__).resolve().parents[2] / "server" / "backend"
    python = server / ".venv" / "bin" / "python"
    if not python.exists():
        pytest.skip("private prompt service environment unavailable")
    database, ready = tmp_path / "server.sqlite3", tmp_path / "ready.json"
    token = "test-token"
    setup = subprocess.run(
        [str(python), "-c", (
            "from pathlib import Path; from server.repository import _PromptRepository; "
            f"r=_PromptRepository(Path({str(database)!r})); "
            f"r.grant({token!r},'tenant-a',('read','librarian')); r.close()"
        )], cwd=server, capture_output=True, text=True,
    )
    assert setup.returncode == 0, setup.stderr
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            str(python), "-m", "server", "serve", "--database", str(database),
            "--ready", str(ready), "--port", str(port),
        ],
        cwd=server, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        endpoint = f"http://127.0.0.1:{json.loads(ready.read_text())['port']}"
        projection = tmp_path / "projection"
        projection.mkdir()
        content = "published exact projection\n"
        digest = hashlib.sha256(content.encode()).hexdigest()
        (projection / "AGENTS.md").write_text(content)
        (projection / "AGENTS.md").chmod(0o644)
        (projection / MANIFEST_NAME).write_text(json.dumps({
            "version": 1, "source_commit": "a" * 40,
            "source_timestamp": "2026-09-05T12:00:00+00:00",
            "outputs": [{
                "path": "AGENTS.md", "sha256": digest, "size": len(content.encode()),
                "mode": "0644", "consumers": ["home-agents"],
            }],
        }))

        publish_projection(projection, endpoint, token)
        publish_projection(projection, endpoint, token)

        request = urllib.request.Request(
            endpoint + "/v1/catalog", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            page = PromptCatalogPage.model_validate(json.loads(response.read()))
        assert len(page.entries) == 1 and page.entries[0].digest == digest
    finally:
        process.terminate()
        process.wait(timeout=3)


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
        namespace="interact-projection",
        slug="output-" + hashlib.sha256(b"AGENTS.md").hexdigest()[:24],
    )
    changed_key = PromptKey(
        namespace="interact-projection",
        slug="output-" + hashlib.sha256(b"instructions.md").hexdigest()[:24],
    )
    removed_key = PromptKey(namespace="interact-projection", slug="removed")
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

    monkeypatch.setattr("interact.prompt_publisher._request", request)
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

    monkeypatch.setattr("interact.prompt_publisher._request", request)
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
