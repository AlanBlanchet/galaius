"""Every workspace this PC reads prompts from, kept as plain files a person can browse.

`<root>/<workspace>/<namespace>/<slug>.md` for each workspace this PC reads: its own, and every
other one that granted this PC its prompts (`GET /v1/machine/prompt-workspaces`). The folders are copies refreshed by `interact prompts sync`,
never a source: a file edited here is overwritten at the next sync, and a change reaches the server
only through `interact prompts write` (its digest check refuses a stale base). A file whose prompt
left its workspace is removed only when this mirror wrote it (its folder's `.mirror.json`).

Folder names come from server text (a workspace's name), so they are reduced to
`[a-z0-9._@+-]`, never `.`/`..`, and two workspaces sharing a name get their id's first 8
characters appended; every folder is reached without following a link (`PinnedDirectory`).
`<root>/personal/` and `<root>/company/`, the layout of earlier releases, are left in place,
read-only, for their owner to remove (`LEGACY`)."""

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter

from interact.agents.catalog_connection import CatalogConnection, CatalogConnectionError
from interact.pinned_directory import PinnedDirectory
from interact.server_prompts import ServerPrompts

MANIFEST = ".mirror.json"
#: Folders an earlier release wrote: kept, never synced again, made read-only once.
LEGACY = ("personal", "company")
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
    """The PC link's own workspace first, then every other workspace that granted this PC."""
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


def folders(workspaces: tuple[PromptWorkspace, ...]) -> dict[UUID, tuple[str]]:
    """The folder of each workspace under the root; a name shared with another workspace or with a
    `LEGACY` folder gets the id's first 8 characters."""
    names = {value.workspace_id: folder_name(value.label) for value in workspaces}
    taken = [*names.values(), *LEGACY]
    return {workspace: (name if taken.count(name) == 1 else f"{name}-{str(workspace)[:8]}",) for workspace, name in names.items()}


def follow_renames(root: Path, places: dict[UUID, tuple[str]]) -> tuple[str, ...]:
    """A workspace renamed since the last sync: its old folder (known by the id its `.mirror.json`
    names) takes the new name; when the new one already exists, the old copy's files this mirror
    wrote go, as for a prompt that left (anything else in it stays, and so does the folder)."""
    current = {str(workspace): place[0] for workspace, place in places.items()}
    notes = []
    for entry in sorted(os.scandir(root), key=lambda item: item.name) if root.is_dir() else ():
        if entry.name in LEGACY or not entry.is_dir(follow_symlinks=False):
            continue
        with PinnedDirectory.at(Path(entry.path)) as folder:
            target = current.get(str(_document(folder).get("workspace_id")))
            if target is None or target == entry.name:
                continue
            if (root / target).exists():
                for relative in sorted(_manifest(folder)):
                    namespace, name = relative.split("/")
                    try:
                        with PinnedDirectory.open(folder.path, namespace) as directory:
                            directory.unlink(name)
                        folder.rmdir(namespace)
                    except OSError:
                        pass  # gone already, or holds something the mirror did not write
                folder.unlink(MANIFEST)
        if (root / target).exists():
            with PinnedDirectory.at(root) as parent:
                try:
                    parent.rmdir(entry.name)
                except OSError:
                    pass
            notes.append(f"{root / entry.name}: its workspace is now {root / target}; the old copy was removed")
        else:
            os.rename(entry.path, root / target)
            notes.append(f"{root / entry.name}: renamed to {root / target} (its workspace was renamed)")
    return tuple(notes)


def freeze_legacy(root: Path) -> tuple[Path, ...]:
    """Make the `LEGACY` folders under `root` read-only (folders 0o500, files 0o400) without
    following a link; nothing is removed. Returns the folders frozen this time."""
    frozen = []
    for name in LEGACY:
        top = root / name
        if not top.is_dir() or top.is_symlink() or not os.stat(top).st_mode & 0o200:
            continue
        for current, directories, files in os.walk(top, topdown=False, followlinks=False):
            for entry in files:
                path = Path(current) / entry
                if not path.is_symlink():
                    path.chmod(0o400)
            for entry in directories:
                path = Path(current) / entry
                if not path.is_symlink():
                    path.chmod(0o500)
        top.chmod(0o500)
        frozen.append(top)
    return tuple(frozen)


class PromptSyncReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    active: UUID
    projection: Path
    notes: tuple[str, ...]


def mirror_root() -> Path:
    configured = os.environ.get("INTERACT_PROMPT_MIRROR_ROOT")
    return Path(configured) if configured else Path.home() / "galaius"


def sync(connection: CatalogConnection, root: Path, install: Callable[[ServerPrompts], Path]) -> PromptSyncReport:
    """Mirror every workspace this PC reads, then install the ACTIVE one's prompts for its agents.
    A PC that never chose, cannot write its own workspace's prompts, and holds exactly one other
    workspace's grant runs on that one; one whose chosen workspace withdrew the grant goes back to its own."""
    values = prompt_workspaces(connection)
    granted = [value for value in values if not value.link]
    readable = {value.workspace_id for value in values}
    notes: list[str] = []
    owned = any(value.link and value.can_write for value in values)
    if not CatalogConnection.path().exists() and len(granted) == 1 and not owned:
        connection = connection.model_copy(update={"workspace_id": granted[0].workspace_id})
        connection.save()
    elif connection.workspace_id not in readable and values:
        notes.append(f"{connection.workspace_id} no longer lets this PC read its prompts; using {values[0].name}")
        connection = connection.model_copy(update={"workspace_id": values[0].workspace_id})
        connection.save()
    places = folders(values)
    _note(root)
    notes.extend(follow_renames(root, places))
    notes.extend(f"{path}: kept read-only (earlier layout), remove it when you no longer need it" for path in freeze_legacy(root))
    for value in values:
        count = mirror(root, connection, value, places[value.workspace_id])
        notes.append(f"{root.joinpath(*places[value.workspace_id])}: {count} prompt(s){' (active)' if value.workspace_id == connection.workspace_id else ''}")
    return PromptSyncReport(active=connection.workspace_id, projection=install(ServerPrompts(connection=connection)), notes=tuple(notes))


def changed(connection: CatalogConnection, seen: dict[UUID, str]) -> bool:
    """Whether any readable workspace appeared, left, or changed its agents or prompts since
    `seen` (workspace -> catalog ETag, updated here): a 304 per unchanged workspace."""
    linked = CatalogConnection.linked()
    if linked is None:
        return False
    values = prompt_workspaces(connection)
    moved = set(seen) != {value.workspace_id for value in values}
    for workspace in set(seen) - {value.workspace_id for value in values}:
        del seen[workspace]
    with linked.connect() as client:
        for value in values:
            response = client.get(f"/v1/workspaces/{value.workspace_id}/agent-catalog", headers={"If-None-Match": seen.get(value.workspace_id, '""')})
            if response.status_code == 200 and response.headers.get("etag"):
                moved = moved or seen.get(value.workspace_id) != response.headers["etag"]
                seen[value.workspace_id] = response.headers["etag"]
    return moved


_NOTE = ("These folders are read-only copies of your prompts, refreshed by `interact prompts sync`\n"
         "(and by this PC's runner when they change). An edit here is overwritten; change a prompt\n"
         "on the platform, or with `interact prompts write`.\n")


def _note(root: Path) -> None:
    with PinnedDirectory.at(root, create=True) as folder:
        _write(folder, "README.txt", _NOTE.encode(), 0o600)


def mirror(root: Path, connection: CatalogConnection, workspace: PromptWorkspace, place: tuple[str, ...]) -> int:
    """Write `workspace`'s current prompts under `root/place`; returns how many files it holds."""
    revisions = ServerPrompts(connection=connection.model_copy(update={"workspace_id": workspace.workspace_id})).catalog()
    wanted = {f"{item.key.namespace}/{item.key.slug}.md": item.content.encode("utf-8") for item in revisions}
    with PinnedDirectory.at(root.joinpath(*place), create=True) as folder:
        previous = _manifest(folder)
        for relative, content in wanted.items():
            namespace, name = relative.split("/")
            with PinnedDirectory.open(folder.path, namespace, create=True) as directory:
                _write(directory, name, content, 0o400)
        for relative in sorted(set(previous) - set(wanted)):
            namespace, name = relative.split("/")
            try:
                with PinnedDirectory.open(folder.path, namespace) as directory:
                    directory.unlink(name)
            except FileNotFoundError:
                pass
        manifest = {"workspace_id": str(workspace.workspace_id), "name": workspace.name,
                    "files": {relative: hashlib.sha256(content).hexdigest() for relative, content in sorted(wanted.items())}}
        _write(folder, MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    return len(wanted)


def _document(folder: PinnedDirectory) -> dict:
    try:
        descriptor = folder.file(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as stream:
        payload = stream.read(1 << 20)
    try:
        document = json.loads(payload)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _manifest(folder: PinnedDirectory) -> dict[str, str]:
    files = _document(folder).get("files", {})
    if not isinstance(files, dict):
        return {}
    return {key: value for key, value in files.items()
            if isinstance(key, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*\.md", key)}


def _write(directory: PinnedDirectory, name: str, content: bytes, mode: int) -> None:
    """Replace `name` atomically with a private file (`mode`; prompt copies read-only), never through a link."""
    partial = f".{name}.{os.getpid()}.partial"
    descriptor = directory.file(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        directory.replace(partial, directory, name)
    except BaseException:
        try:
            directory.unlink(partial)
        except FileNotFoundError:
            pass
        raise
