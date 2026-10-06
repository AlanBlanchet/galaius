"""Every workspace this PC reads prompts from, kept as plain files a person can browse.

`<root>/personal/<email>/<namespace>/<slug>.md` for the owner's personal workspace and
`<root>/company/<company>/<namespace>/<slug>.md` for each company that granted this PC its prompts
(`GET /v1/machine/prompt-workspaces`). The folders are copies refreshed by `interact prompts sync`,
never a source: a file edited here is overwritten at the next sync, and a change reaches the server
only through `interact prompts write` (its digest check refuses a stale base). A file whose prompt
left its workspace is removed only when this mirror wrote it (its folder's `.mirror.json`).

Folder names come from server text (an email, a company name), so they are reduced to
`[a-z0-9._@+-]`, never `.`/`..`, and two companies sharing a name get their id's first 8
characters appended; every folder is reached without following a link (`PinnedDirectory`)."""

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter

from interact.agents.catalog_connection import CatalogConnection, CatalogConnectionError
from interact.pinned_directory import PinnedDirectory
from interact.server_prompts import ServerPrompts

MANIFEST = ".mirror.json"
_MAX_WORKSPACES = 64


class PromptWorkspace(BaseModel):
    """One workspace this PC may read prompts from (the server's `MachinePromptWorkspace`)."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    workspace_id: UUID
    name: str
    kind: Literal["personal", "company"]
    label: str
    link: bool
    can_write: bool
    write_until: datetime | None = None


def prompt_workspaces(connection: CatalogConnection) -> tuple[PromptWorkspace, ...]:
    """The PC link's own workspace first, then every company that granted this PC."""
    linked = CatalogConnection.linked()
    if linked is None or connection.auth_mode != "machine":
        raise CatalogConnectionError("only a linked PC lists the workspaces it may read prompts from")
    with linked.connect() as client:
        payload = linked.request(client, "GET", "/v1/machine/prompt-workspaces")
    try:
        values = TypeAdapter(tuple[PromptWorkspace, ...]).validate_json(payload)
    except ValueError as error:
        raise CatalogConnectionError("invalid prompt workspace list") from error
    if len(values) > _MAX_WORKSPACES:
        raise CatalogConnectionError("prompt workspace list exceeds its limit")
    return values


def folder_name(text: str) -> str:
    """`text` as one safe folder name: ascii, lowercase, `[a-z0-9._@+-]`, no leading dot."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"\.{2,}", ".", re.sub(r"[^a-z0-9._@+-]+", "-", ascii_text)).strip(".-")[:80].strip(".-")
    return name or "workspace"


def folders(workspaces: tuple[PromptWorkspace, ...]) -> dict[UUID, tuple[str, str]]:
    """(kind folder, own folder) per workspace; a shared name gets the id's first 8 characters."""
    names = {value.workspace_id: folder_name(value.label) for value in workspaces}
    taken = [(value.kind, names[value.workspace_id]) for value in workspaces]
    return {value.workspace_id: (value.kind, names[value.workspace_id] if taken.count((value.kind, names[value.workspace_id])) == 1
                                 else f"{names[value.workspace_id]}-{str(value.workspace_id)[:8]}") for value in workspaces}


def mirror(root: Path, connection: CatalogConnection, workspace: PromptWorkspace, place: tuple[str, str]) -> int:
    """Write `workspace`'s current prompts under `root/place`; returns how many files it holds."""
    revisions = ServerPrompts(connection=connection.model_copy(update={"workspace_id": workspace.workspace_id})).catalog()
    wanted = {f"{item.key.namespace}/{item.key.slug}.md": item.content.encode("utf-8") for item in revisions}
    with PinnedDirectory.at(root.joinpath(*place), create=True) as folder:
        previous = _manifest(folder)
        for relative, content in wanted.items():
            namespace, name = relative.split("/")
            with PinnedDirectory.open(folder.path, namespace, create=True) as directory:
                _write(directory, name, content)
        for relative in sorted(set(previous) - set(wanted)):
            namespace, name = relative.split("/")
            try:
                with PinnedDirectory.open(folder.path, namespace) as directory:
                    directory.unlink(name)
            except FileNotFoundError:
                pass
        manifest = {"workspace_id": str(workspace.workspace_id), "name": workspace.name,
                    "files": {relative: hashlib.sha256(content).hexdigest() for relative, content in sorted(wanted.items())}}
        _write(folder, MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode())
    return len(wanted)


def _manifest(folder: PinnedDirectory) -> dict[str, str]:
    try:
        descriptor = folder.file(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as stream:
        payload = stream.read(1 << 20)
    try:
        files = json.loads(payload).get("files", {})
    except (ValueError, AttributeError):
        return {}
    return {key: value for key, value in files.items()
            if isinstance(key, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*\.md", key)}


def _write(directory: PinnedDirectory, name: str, content: bytes) -> None:
    """Replace `name` atomically with a private file (0600), never through a link."""
    partial = f".{name}.{os.getpid()}.partial"
    descriptor = directory.file(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        directory.replace(partial, directory, name)
    except BaseException:
        try:
            directory.unlink(partial)
        except FileNotFoundError:
            pass
        raise
