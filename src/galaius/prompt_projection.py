"""Compile exact committed prompt sources into verified provider projections."""

import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
from uuid import UUID, uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

import yaml
from galaius_core import PromptExecutionRef, PromptKey

from galaius.agents.catalog import AgentCatalog, AgentInstructionSet
from galaius.agents.catalog_connection import CatalogConnection
from galaius.agents.policy import TOOL_PREFIX
from galaius.agents.tool_gateway import AgentToolList, ToolGateway
from galaius.file_lock import exclusive
from galaius.pinned_directory import PinnedDirectory


MANIFEST_NAME = "projection-manifest.json"
_OUTPUT_ROOTS = ("agents", "skills", "rules", "scopes", "references")
_OUTPUT_FILES = ("AGENTS.md", "instructions.md", "org.json")
_PASSTHROUGH_ROOTS = ("hooks",)


def compile_server_prompt_projection(connection: CatalogConnection, installed_root: Path) -> Path:
    """Compile one verified server snapshot without executing downloaded source code."""
    catalog = AgentCatalog.refresh(connection, allow_stale=False)
    gateway = ToolGateway.current(online=True)
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    consumer_key = hashlib.sha256(str(installed_root.resolve()).encode()).hexdigest()[:16]
    root = cache_home / "galaius" / "prompts" / "server" / str(connection.workspace_id)
    # The gateway's revision names the projection too: a tool connected or removed on the server
    # changes the roles' tools with no new catalog cursor.
    destination = root / "-".join((catalog.snapshot.cursor, consumer_key, str(catalog.access_generation), *((gateway.revision[:16],) if gateway.revision else ())))
    _safe_directory(root, create=True)
    outputs = _server_outputs(catalog, installed_root, destination, gateway)
    if destination.exists():
        with connection.access_guard(catalog.access_generation):
            _verified_server_cache(destination, outputs)
            return destination
    staging = root / f".compile-{uuid4().hex}"
    with PinnedDirectory.at(root) as parent:
        parent.mkdir(staging.name)
    try:
        for relative, content in outputs.items():
            path = staging / _safe_path(relative)
            _safe_directory(path.parent, create=True)
            _write_prompt(path, content.encode("utf-8"), 0o600)
        records = _compiled_outputs(staging)
        manifest = {
            "version": 1, "source_kind": "server-catalog", "source_commit": None,
            "workspace_id": str(catalog.connection.workspace_id),
            "catalog_cursor": catalog.snapshot.cursor,
            "access_generation": str(catalog.access_generation),
            "fetched_at": catalog.fetched_at.isoformat(),
            "outputs": [{"path": name, "mode": f"{mode:04o}", "size": len(content),
                         "sha256": hashlib.sha256(content).hexdigest(),
                         "consumers": _consumer_kinds(name)}
                        for name, (mode, content) in sorted(records.items())],
        }
        _write_prompt(staging / MANIFEST_NAME, (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
        with connection.access_guard(catalog.access_generation):
            if destination.exists():
                _verified_server_cache(destination, outputs)
            else:
                _move_prompt_backup(staging, destination)
        return destination
    finally:
        if staging.exists():
            with PinnedDirectory.at(root) as parent:
                parent.rmtree(staging.name)


def _verified_server_cache(destination: Path, outputs: dict[str, str]) -> None:
    actual = _validated_manifest_outputs(destination)
    if {name: path.read_bytes() for name, path in actual.items()} != {
        name: content.encode("utf-8") for name, content in outputs.items()
    }:
        raise ValueError("cached projection differs from verified server records")


def install_server_prompt_projection(
    connection: CatalogConnection, home: Path, vscode_root: Path, state_path: Path,
    adoption_path: Path | None = None,
) -> Path:
    """Install server-derived prompt files while leaving runtime hooks and settings intact."""
    projection = compile_server_prompt_projection(connection, home)
    manifest = json.loads((projection / MANIFEST_NAME).read_text())
    generation = UUID(manifest["access_generation"])
    with connection.access_guard(generation):
        install_prompt_projection(projection, home, vscode_root, state_path, adoption_path)
    return projection


def _listable_tools(role: str, tools) -> list[str]:
    """The tools a role's file may LIST, refused when this runtime cannot dispatch one of them.

    A projection is where a role's tool names — authored on the server, ahead of any runtime —
    are handed to a model as fact. The names it can honour are the ones this galaius's MCP
    server registers, so that ONE set decides both: a listed tool is a callable tool, or the
    install fails naming the gap. Silence here reads to the agent as "No such tool available"
    mid-task, with the tool still in its prompt (#224).

    Tool names belonging to the harness or another server pass through untouched — not ours.
    """
    names = list(tools)
    if not any(name.startswith(TOOL_PREFIX) for name in names):
        return names
    from galaius import __version__
    from galaius.server import undispatchable_tools  # registers every tool on import

    missing = undispatchable_tools(names)
    if missing:
        raise ValueError(
            f"role {role!r} lists galaius tools this runtime cannot dispatch: "
            f"{', '.join(missing)} — galaius {__version__} serves "
            f"{TOOL_PREFIX}… tools that do not include them; upgrade galaius, or correct the "
            "role's tools on the server"
        )
    return names


def _server_outputs(catalog: AgentCatalog, home: Path, projection: Path, gateway: AgentToolList = AgentToolList()) -> dict[str, str]:
    snapshot = catalog.snapshot
    roles = sorted(snapshot.agents, key=lambda agent: agent.role_key or str(agent.id))
    identifiers = {agent.id: agent.role_key or f"agent-{agent.id}" for agent in roles}
    if snapshot.root_agent is None:
        raise ValueError("choose the workspace's main agent before installing its instructions")
    main = snapshot.revision(snapshot.root_agent)
    if not snapshot.prompt_heads:
        raise ValueError("server catalog lacks authoritative prompt heads; upgrade the server first")
    heads = {reference.key: snapshot.prompt(reference) for reference in snapshot.prompt_heads}
    outputs: dict[str, str] = {}
    skill_paths = {}
    skill_discovery = {}
    for agent in roles:
        for reference in agent.skill_paradigms:
            prompt = snapshot.prompt(reference)
            metadata = _projection_metadata(prompt.content)
            if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
                raise ValueError(f"server skill {prompt.key.namespace}/{prompt.key.slug} lacks routing metadata")
            ProjectionMetadata.from_content(prompt.content)
            relative = f"references/{prompt.key.namespace}/{prompt.key.slug}/{prompt.digest}/SKILL.md"
            outputs[relative] = prompt.content
            skill_paths[reference] = projection / relative
            head = heads.get(prompt.key)
            if head is None:
                raise ValueError("server skill has no authoritative head")
            skill_discovery[prompt.key] = head
    for prompt in skill_discovery.values():
        metadata = ProjectionMetadata.from_content(prompt.content)
        routing = metadata.galaius
        if routing is None or routing.projection != "skill":
            raise ValueError("server skill head is missing its skill routing")
        relative = _projection_relative("skills", f"{prompt.key.slug}/SKILL.md", routing.scope)
        if relative in outputs:
            raise ValueError("server skills have colliding consumer names")
        outputs[relative] = prompt.content
    for agent in roles:
        if agent.id == main.id:
            continue
        key = identifiers[agent.id]
        header = {"name": key, "description": agent.description or agent.name,
                  "tools": _listable_tools(key, gateway.granted(key, agent.harness_tools, TOOL_PREFIX))}
        body = snapshot.definition(key, skill_paths, instructions=AgentInstructionSet(agent=agent, prompts=snapshot.paradigms))
        outputs[_projection_relative("agents", f"{key}.md", agent.scope)] = (
            "---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True) + "---\n\n" + body + "\n"
        )
    rule_references = []
    for prompt in snapshot.paradigms:
        metadata = _projection_metadata(prompt.content)
        if metadata.get("galaius", {}).get("projection") == "rule":
            ProjectionMetadata.from_content(prompt.content)
            outputs[f"references/{prompt.key.namespace}/{prompt.key.slug}/{prompt.digest}/RULE.md"] = prompt.content
    for prompt in heads.values():
        metadata = _projection_metadata(prompt.content)
        if metadata.get("galaius", {}).get("projection") != "rule":
            continue
        routing = ProjectionMetadata.from_content(prompt.content).galaius
        relative = _projection_relative("rules", f"{prompt.key.slug}.md", routing.scope)
        if relative in outputs:
            raise ValueError("server rules have colliding consumer names")
        header = {key: value for key, value in metadata.items() if key != "galaius"}
        if routing.paths:
            header["paths"] = list(routing.paths)
        outputs[relative] = "---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True) + "---\n\n" + AgentInstructionSet.instruction_body(prompt) + "\n"
        if routing.scope == "core":
            reference = projection / f"references/{prompt.key.namespace}/{prompt.key.slug}/{prompt.digest}/RULE.md"
            condition = f"Read when working on files matching {', '.join(routing.paths)}" if routing.paths else "Read when relevant to the current task"
            rule_references.append(f"- {condition}: {reference}")
    for filename, provider, consumer in (("AGENTS.md", "openai", ".codex"),
                                         ("instructions.md", "anthropic", ".claude")):
        provider_prompt = heads.get(PromptKey(namespace="paradigms", slug=f"provider-{provider}"))
        if provider_prompt is None:
            raise ValueError(f"server catalog lacks provider-{provider} instructions")
        provider_ref = PromptExecutionRef(key=provider_prompt.key, channel="stable",
                                         digest=provider_prompt.digest, revision=provider_prompt.revision)
        paradigms = tuple(ref for ref in main.paradigms if not (
            ref.key.namespace == "paradigms" and ref.key.slug.startswith("provider-")
        )) + (provider_ref,)
        source = AgentInstructionSet(agent=main.model_copy(update={"paradigms": paradigms}),
                                     prompts=snapshot.paradigms)
        body = snapshot.definition(identifiers[main.id], skill_paths, instructions=source)
        if rule_references:
            body += "\n\nRule reference files (respect each path condition):\n" + "\n".join(sorted(rule_references))
        outputs[filename] = "<!-- Derived from server agent revisions; edit through Galaius. -->\n\n" + body + "\n"
    providers = {name: {"label": label, "binary": name, "env": shutil.which(name) is not None}
                 for name, label in (("claude", "Claude Code"), ("codex", "Codex"))}
    outputs["org.json"] = json.dumps({
        "coordinator": {"id": identifiers[main.id], "title": main.name}, "providers": providers,
        "departments": [{"id": name} for name in sorted({agent.department for agent in roles if agent.department})],
        "agents": [{"name": identifiers[agent.id], "title": agent.name, "description": agent.description,
                    "department": agent.department, "scope": agent.scope,
                    "reports_to": identifiers.get(agent.reports_to),
                    "providers": list(providers), "def": str(home / ".claude" / _projection_relative(
                        "agents", f"{identifiers[agent.id]}.md", agent.scope))}
                   for agent in roles if agent.id != main.id],
        "catalog_cursor": snapshot.cursor,
    }, indent=2, ensure_ascii=False) + "\n"
    return outputs


class ProjectionRouting(BaseModel):
    """Server-owned discovery scope and path conditions, shared by skills and rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    projection: Literal["skill", "rule"]
    scope: str = "core"
    paths: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def valid_paths(self):
        PromptKey(namespace="scope", slug=self.scope)
        if any(not path or len(path) > 1024 or any(char in path for char in "\r\n\x00") for path in self.paths):
            raise ValueError("rule path conditions must be bounded single-line globs")
        return self


class ProjectionMetadata(BaseModel):
    """Descriptive prompt headers cannot grant tools or configure runtime execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    galaius: ProjectionRouting | None = None
    paths: tuple[str, ...] = ()

    @classmethod
    def from_content(cls, content: str):
        try:
            return cls.model_validate(_projection_metadata(content))
        except ValueError as error:
            raise ValueError("server projection metadata permits only name, description and typed routing") from error

    @model_validator(mode="after")
    def valid_paths(self):
        # Consumer rules lift the same path conditions out of galaius routing.
        ProjectionRouting(projection="rule", paths=self.paths)
        return self


def _projection_metadata(content: str) -> dict:
    content = content.removeprefix("\ufeff").replace("\r\n", "\n")
    if not content.startswith("---\n"):
        return {}
    pieces = content.split("\n---\n", 1)
    if len(pieces) != 2:
        raise ValueError("invalid server prompt frontmatter")
    try:
        metadata = yaml.safe_load(pieces[0][4:])
    except yaml.YAMLError as error:
        raise ValueError("invalid server prompt frontmatter") from error
    if not isinstance(metadata, dict) or not isinstance(metadata.get("galaius", {}), dict):
        raise ValueError("invalid server prompt metadata")
    return metadata


def _projection_relative(kind: str, filename: str, scope: str) -> str:
    PromptKey(namespace="scope", slug=scope)
    return f"{kind}/{filename}" if scope == "core" else f"scopes/{scope}/{kind}/{filename}"


def compile_prompt_projection(
    repository: Path,
    commit: str,
    projection_root: Path,
    installed_root: Path,
    generator_path: str = "generate.py",
) -> Path:
    """Compile twice from one Git commit, publishing only byte-identical outputs."""
    if projection_root.exists():
        raise ValueError("projection destination already exists")
    projection_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if projection_root.parent.is_symlink() or not projection_root.parent.is_dir():
        raise ValueError("projection parent is not a safe directory")
    resolved_commit = _git(repository, "rev-parse", f"{commit}^{{commit}}").strip()
    entries = _source_entries(repository, resolved_commit)
    generator = _safe_path(generator_path)
    if generator.as_posix() not in entries:
        raise ValueError("generator is absent from committed source")
    work_roots = (
        projection_root.parent / f".{projection_root.name}.compile-a",
        projection_root.parent / f".{projection_root.name}.compile-b",
    )
    if any(path.exists() for path in work_roots):
        raise ValueError("compiler workspace already exists")
    try:
        for root in work_roots:
            _materialize(repository, entries, root)
            result = subprocess.run(
                [sys.executable, generator.as_posix()], cwd=root, capture_output=True,
                env={"PATH": os.environ["PATH"], "_GENERATE_YAML_BOOTSTRAP": "1"},
                timeout=30,
            )
            if result.returncode:
                raise ValueError("committed prompt generator failed")
            _normalize_output_modes(root)
            _copy_passthrough_outputs(root, entries)
        first = _compiled_outputs(work_roots[0])
        second = _compiled_outputs(work_roots[1])
        if first != second:
            raise ValueError("prompt generator is not deterministic")
        projection_root.mkdir(mode=0o755)
        for relative, (mode, content) in first.items():
            destination = projection_root / relative
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(mode)
        return _write_manifest(
            repository, resolved_commit, projection_root, installed_root, generator, first
        )
    finally:
        for root in work_roots:
            if root.exists():
                shutil.rmtree(root)


def stage_projection_install(
    projection_root: Path, installed_root: Path, staging_root: Path
) -> Path:
    """Verify a projection manifest and prepare private files without switching consumers."""
    if staging_root.exists():
        raise ValueError("install staging destination already exists")
    manifest = json.loads((projection_root / MANIFEST_NAME).read_text(encoding="utf-8"))
    staging_root.mkdir(mode=0o700)
    try:
        for output in manifest["outputs"]:
            relative = _safe_path(output["path"])
            source = projection_root / relative
            if source.is_symlink() or not source.is_file():
                raise ValueError("projection output is not a regular file")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != output["sha256"]:
                raise ValueError("projection output digest changed")
            destination = staging_root / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(0o600)
        return staging_root
    except Exception:
        shutil.rmtree(staging_root)
        raise


def install_prompt_projection(
    projection_root: Path, home: Path, vscode_root: Path, state_path: Path,
    adoption_path: Path | None = None,
) -> None:
    """Transactionally switch only manifest-owned provider consumer files."""
    # Access guards serialize one connection; this lock serializes the shared
    # consumer state even when two different connections install into it.
    _safe_directory(state_path.parent, create=True)
    lock = state_path.with_name(f".{state_path.name}.lock")
    with PinnedDirectory.at(lock.parent) as parent:
        descriptor = parent.file(lock.name, os.O_CREAT | os.O_RDWR, 0o600)
    with exclusive(descriptor):
        _install_prompt_projection(projection_root, home, vscode_root, state_path, adoption_path)


def _install_prompt_projection(
    projection_root: Path, home: Path, vscode_root: Path, state_path: Path,
    adoption_path: Path | None,
) -> None:
    transaction = state_path.parent / ".prompt-install-transaction"
    if transaction.exists() or transaction.is_symlink():
        raise ValueError(f"prompt install requires recovery of preserved transaction: {transaction}")
    outputs = _validated_manifest_outputs(projection_root)
    manifest = json.loads((projection_root / MANIFEST_NAME).read_text(encoding="utf-8"))
    server_owned = manifest.get("source_kind") == "server-catalog"
    state = _installed_document(state_path)
    adoption = _adoption_manifest(adoption_path)
    settings = (home / ".claude" / "settings.json", home / ".codex" / "hooks.json",
                home / ".cursor" / "hooks.json")
    settings_before = {path: _entry_identity(path, symlink_target=adoption.get(path, (None, None))[0])
                       for path in settings} if not server_owned else {}
    targets, mergeable, hook_groups = _consumer_payloads(
        outputs, home, vscode_root, state.get("hook_groups", {}), prompt_only=server_owned,
    )
    previous = {Path(target): digest for target, digest in state.get("managed", {}).items()}
    # Hooks are installed runtime code. A prompt-only snapshot cannot remove or rewrite them.
    retained = {}
    if server_owned:
        runtime_roots = (home / ".claude" / "hooks",)
        runtime_files = {home / ".claude" / "settings.json", home / ".codex" / "hooks.json",
                         home / ".cursor" / "hooks.json"}
        retained = {target: digest for target, digest in previous.items()
                    if target in runtime_files or any(root in target.parents for root in runtime_roots)}
        previous = {target: digest for target, digest in previous.items() if target not in retained}
    adopted_directories = _adopted_directories(targets, adoption, home, vscode_root)
    observed = {directory: _entry_identity(directory, symlink_target=adoption[directory][0])
                for directory in adopted_directories}
    if any((identity[3], identity[4]) != adoption[directory]
           for directory, identity in observed.items()):
        raise ValueError("legacy directory symlink does not match adoption manifest")
    for target in set(targets) | set(previous):
        if any(directory in target.parents for directory in adopted_directories):
            observed[target] = None
            continue
        _require_safe_parent(target, home, vscode_root)
        observed[target] = _entry_identity(target, symlink_target=adoption.get(target, (None, None))[0])
        if target in mergeable and observed[target] != settings_before[target]:
            raise ValueError("prompt settings changed while preparing install")
        if observed[target] is None:
            continue
        if target.is_symlink():
            declared = adoption.get(target)
            if declared is None or (observed[target][3], observed[target][4]) != declared:
                raise ValueError("legacy symlink does not match adoption manifest")
            continue
        if not target.is_file():
            raise ValueError("managed prompt target is not a regular file")
        current = observed[target][-1]
        expected = previous.get(target)
        if expected is None and target not in mergeable:
            raise ValueError("unmanaged prompt target collision")
        if expected is not None and current != expected and target not in mergeable:
            raise ValueError("managed prompt target changed locally")
    with PinnedDirectory.at(transaction.parent) as parent:
        parent.mkdir(transaction.name)
    backups: dict[Path, Path] = {}
    installed = {}
    created_directories = []
    # Retain the target/backup mapping even if the process is interrupted. This
    # is a recovery inventory, not a claim of power-loss atomicity.
    ordered = sorted(set(targets) | set(previous), key=str)
    directories = sorted(adopted_directories, key=lambda path: len(path.parts))
    recovery = {
        "state": str(state_path),
        "backups": {**{str(path): f"adopted-{index}" for index, path in enumerate(directories)},
                    **{str(path): f"backup-{index}" for index, path in enumerate(ordered)}},
    }
    _write_prompt(transaction / "recovery.json", (json.dumps(recovery, indent=2) + "\n").encode(), 0o600)
    preserve = False
    try:
        for index, directory in enumerate(directories):
            backup = transaction / f"adopted-{index}"
            _move_prompt_backup(directory, backup)
            backups[directory] = backup
            if _entry_identity(backup, symlink_parent=directory.parent,
                               symlink_target=adoption[directory][0]) != observed[directory]:
                raise ValueError("adopted prompt directory changed during install")
            with PinnedDirectory.at(directory.parent) as parent:
                parent.mkdir(directory.name)
            created_directories.append(directory)
        for index, target in enumerate(ordered):
            _require_safe_parent(target, home, vscode_root)
            created_directories.extend(_safe_directory(target.parent, create=True))
            if observed[target] is not None:
                backup = transaction / f"backup-{index}"
                _move_prompt_backup(target, backup)
                backups[target] = backup
                if _entry_identity(backup, symlink_parent=target.parent,
                                   symlink_target=adoption.get(target, (None, None))[0]) != observed[target]:
                    raise ValueError("managed prompt target changed during install")
            payload = targets.get(target)
            if payload is not None:
                staged = transaction / f"new-{index}"
                _write_prompt(staged, *payload)
                # Hard-link publication fails if another writer filled the gap;
                # replace() would silently destroy that writer's new file.
                identity = _entry_identity(staged)
                copied = _publish_prompt(staged, target)
                installed[target] = identity if copied is None else (
                    copied.st_dev, copied.st_ino, copied.st_mode, identity[-1])
        if any(_entry_identity(path) != identity for path, identity in installed.items()):
            raise ValueError("managed prompt target changed during install")
        if any(_entry_identity(backup, symlink_parent=path.parent,
                               symlink_target=adoption.get(path, (None, None))[0]) != observed[path]
               for path, backup in backups.items()):
            raise ValueError("managed prompt backup changed during install")
        state_staged = transaction / "state"
        _write_prompt(state_staged, (json.dumps({
            "managed": {**{str(path): digest for path, digest in retained.items()}, **{
                str(path): hashlib.sha256(targets[path][0]).hexdigest()
                for path in sorted(targets, key=str)
            }},
            "source_commit": manifest["source_commit"],
            "source_kind": manifest.get("source_kind", "git"),
            "catalog_cursor": manifest.get("catalog_cursor"),
            "hook_groups": hook_groups,
            "version": 1,
        }, indent=2, sort_keys=True) + "\n").encode(), 0o600)
        _move_prompt_backup(state_staged, state_path)
    except BaseException as error:
        # A failure in recovery itself must never enable cleanup of the backups.
        preserve = True
        recovery["rollback"] = {str(path): f"rollback-{index}"
                                for index, path in enumerate(reversed(installed))}
        recovery_staged = transaction / "recovery-next.json"
        _write_prompt(recovery_staged, (json.dumps(recovery, indent=2) + "\n").encode(), 0o600)
        _move_prompt_backup(recovery_staged, transaction / "recovery.json")
        preserve = False
        # Move first, then check identity: a check followed by unlink still
        # deletes a replacement made between those two operations.
        for index, (target, identity) in enumerate(reversed(installed.items())):
            removed = transaction / f"rollback-{index}"
            try:
                if _entry_identity(target) != identity:
                    preserve = True
                    continue
                _move_prompt_backup(target, removed)
                if _entry_identity(removed) != identity:
                    _restore_prompt_backup(removed, target)
                    preserve = True
                else:
                    with PinnedDirectory.at(removed.parent) as parent:
                        parent.unlink(removed.name)
            except (OSError, ValueError):
                preserve = True
        for directory in reversed(created_directories):
            try:
                with PinnedDirectory.at(directory.parent) as parent:
                    parent.rmdir(directory.name)  # Never remove concurrent children.
            except (OSError, ValueError):
                preserve = True
        for target, backup in reversed(backups.items()):
            try:
                _restore_prompt_backup(backup, target)
            except (OSError, ValueError):
                preserve = True
        if preserve:
            raise ValueError(
                f"prompt install stopped; operator content and backups preserved for recovery: {transaction}"
            ) from error
        raise
    finally:
        if transaction.exists() and not preserve:
            with PinnedDirectory.at(transaction.parent) as parent:
                parent.rmtree(transaction.name)


def _restore_prompt_backup(backup: Path, target: Path) -> None:
    _publish_prompt(backup, target)
    with PinnedDirectory.at(backup.parent) as parent:
        parent.unlink(backup.name)


def _publish_prompt(source: Path, target: Path) -> os.stat_result | None:
    """None: `target` is `source` under a second name; else the facts of the copy made instead."""
    with PinnedDirectory.at(source.parent) as source_parent, PinnedDirectory.at(target.parent) as target_parent:
        return source_parent.link(source.name, target_parent, target.name)


def _write_prompt(path: Path, content: bytes, mode: int) -> None:
    with PinnedDirectory.at(path.parent) as parent:
        descriptor = parent.file(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            parent.chmod(stream.fileno(), mode)


def _move_prompt_backup(source: Path, backup: Path) -> None:
    with PinnedDirectory.at(source.parent) as source_parent, PinnedDirectory.at(backup.parent) as backup_parent:
        source_parent.replace(source.name, backup_parent, backup.name)


def _entry_identity(path: Path, *, symlink_parent: Path | None = None, symlink_target: str | None = None):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        link = os.readlink(path)
        if link != symlink_target:
            raise ValueError("legacy symlink does not match adoption manifest")
        referent = (symlink_parent or path.parent) / link
        content = _directory_digest(referent) if referent.is_dir() else hashlib.sha256(referent.read_bytes()).hexdigest()
        return (info.st_dev, info.st_ino, info.st_mode, link, content)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("managed prompt target is not a regular file")
    with PinnedDirectory.at(path.parent) as parent:
        with os.fdopen(parent.file(path.name), "rb") as stream:
            info = os.fstat(stream.fileno())
            content = stream.read()
    return (info.st_dev, info.st_ino, info.st_mode, hashlib.sha256(content).hexdigest())


def _safe_directory(path: Path, *, create: bool = False) -> list[Path]:
    """The folders made (outermost first); ValueError when a link or a file stands on `path`."""
    try:
        with PinnedDirectory.at(path, create=create) as directory:
            return list(directory.created)
    except FileNotFoundError:
        if create:
            raise
        return []
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"prompt path is not a safe directory: {path}") from error
        raise


def _validated_manifest_outputs(projection_root: Path) -> dict[str, Path]:
    _safe_directory(projection_root)
    if (projection_root / MANIFEST_NAME).is_symlink():
        raise ValueError("projection manifest is not a regular file")
    manifest = json.loads((projection_root / MANIFEST_NAME).read_text(encoding="utf-8"))
    declared: dict[str, tuple[str, int, str]] = {}
    for output in manifest.get("outputs", []):
        relative = _safe_path(output["path"]).as_posix()
        if relative in declared or relative.casefold() in {key.casefold() for key in declared}:
            raise ValueError("projection manifest output collision")
        declared[relative] = (output["sha256"], output["size"], output["mode"])
    actual = _compiled_outputs(projection_root)
    if set(actual) != set(declared):
        raise ValueError("projection manifest output set changed")
    for relative, (mode, content) in actual.items():
        digest, size, declared_mode = declared[relative]
        if (hashlib.sha256(content).hexdigest(), len(content), f"{mode:04o}") != (
            digest, size, declared_mode
        ):
            raise ValueError("projection manifest output identity changed")
        if manifest.get("source_kind") == "server-catalog":
            path = PurePosixPath(relative)
            if (path.parts[0] in {"skills", "rules"}
                    or (_scoped_output(path) is not None and path.parts[2] in {"skills", "rules"})
                    or (path.parts[0] == "references" and path.name in {"SKILL.md", "RULE.md"})):
                ProjectionMetadata.from_content(content.decode("utf-8"))
    return {relative: projection_root / relative for relative in actual}


def _consumer_payloads(
    outputs: dict[str, Path], home: Path, vscode_root: Path,
    prior_hook_groups: dict[str, list[object]], *, prompt_only: bool = False,
) -> tuple[dict[Path, tuple[bytes, int]], set[Path], dict[str, list[object]]]:
    targets: dict[Path, tuple[bytes, int]] = {}
    hook_scripts = {
        PurePosixPath(relative).name
        for relative in outputs
        if relative.startswith("hooks/") and relative.endswith(".sh")
    }
    for relative, source in outputs.items():
        path = PurePosixPath(relative)
        destinations: tuple[Path, ...]
        if relative == "AGENTS.md":
            # Codex reads the GLOBAL AGENTS.md at $CODEX_HOME, and the repo chain
            # from the git root down to cwd — never ~/AGENTS.md for a project
            # outside $HOME, so the home copy alone reached no Codex session.
            destinations = (home / "AGENTS.md", home / ".codex" / "AGENTS.md")
        elif relative == "instructions.md":
            # Only the user-level file: a copy at ~/CLAUDE.md was loaded a SECOND
            # time as an ancestor project file by every project under $HOME.
            destinations = (
                home / ".claude" / "CLAUDE.md", vscode_root / "alan.instructions.md",
            )
        elif relative == "org.json":
            destinations = (home / ".claude" / "org.json",)
        elif len(path.parts) == 2 and path.parts[0] == "agents":
            destinations = (
                home / ".claude" / "agents" / path.name,
                vscode_root / f"{path.stem}.agent.md",
            )
        elif len(path.parts) == 3 and path.parts[0] == "skills" and path.name == "SKILL.md":
            name = path.parts[1]
            destinations = (
                home / ".claude" / "skills" / name / "SKILL.md",
                home / ".codex" / "skills" / name / "SKILL.md",
                vscode_root / f"{name}.skill.instructions.md",
            )
        elif len(path.parts) == 2 and path.parts[0] == "rules":
            destinations = (
                home / ".claude" / "rules" / path.name,
                vscode_root / f"{path.stem}.instructions.md",
            )
            if prompt_only:
                destinations += (home / ".codex" / "rules" / path.name,)
        elif path.parts[0] == "references":
            # Immutable skill references are already in the verified projection cache.
            continue
        elif _scoped_output(path) is not None:
            # Domain-scoped agents / skills are HELD outside every consumer's
            # auto-load roots; `galaius prompts scope <name>` links a set into
            # one project's .claude/ so a coding project never loads another domain's agents.
            destinations = (home / ".claude" / "scopes" / Path(*path.parts[1:]),)
        elif len(path.parts) == 2 and path.parts[0] == "hooks" and path.suffix == ".sh":
            destinations = (home / ".claude" / "hooks" / path.name,)
        elif relative in {"hooks/hooks.json", "hooks/turn-end-asks.md", "hooks/codex-hooks.json", "hooks/cursor-hooks.json"}:
            continue
        else:
            raise ValueError("projection output has no consumer mapping")
        for destination in destinations:
            if destination in targets:
                raise ValueError("prompt consumer target collision")
            content = source.read_bytes()
            if prompt_only and len(path.parts) == 2 and path.parts[0] == "rules" and destination.parent == vscode_root:
                metadata = _projection_metadata(content.decode())
                paths = metadata.pop("paths", ())
                if paths:
                    metadata["applyTo"] = ",".join(paths)
                body = content.decode().split("\n---\n", 1)[1]
                content = ("---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True) + "---\n" + body).encode()
            targets[destination] = (content, 0o700 if path.suffix == ".sh" else 0o600)
    if prompt_only:
        return targets, set(), prior_hook_groups
    settings = home / ".claude" / "settings.json"
    settings_bytes, hook_groups = _merged_hook_settings(
        outputs, settings, hook_scripts, prior_hook_groups
    )
    targets[settings] = (settings_bytes, 0o600)
    mergeable = {settings}
    if "hooks/codex-hooks.json" in outputs:
        codex_settings = home / ".codex" / "hooks.json"
        current = json.loads(codex_settings.read_text()) if codex_settings.exists() else {}
        fragment = json.loads(outputs["hooks/codex-hooks.json"].read_text())
        hooks = current.setdefault("hooks", {})
        for event, additions in fragment["hooks"].items():
            previous = prior_hook_groups.get(f"codex:{event}", [])
            hooks[event] = [group for group in hooks.get(event, [])
                            if group not in previous and group not in additions] + additions
            hook_groups[f"codex:{event}"] = additions
        targets[codex_settings] = ((json.dumps(current, indent=2) + "\n").encode(), 0o600)
        mergeable.add(codex_settings)
    if "hooks/cursor-hooks.json" in outputs:
        cursor_settings = home / ".cursor" / "hooks.json"
        current = json.loads(cursor_settings.read_text()) if cursor_settings.exists() else {"version": 1}
        fragment = json.loads(outputs["hooks/cursor-hooks.json"].read_text())
        hooks = current.setdefault("hooks", {})
        for event, additions in fragment["hooks"].items():
            previous = prior_hook_groups.get(f"cursor:{event}", [])
            hooks[event] = [group for group in hooks.get(event, [])
                            if group not in previous and group not in additions] + additions
            hook_groups[f"cursor:{event}"] = additions
        targets[cursor_settings] = ((json.dumps(current, indent=2) + "\n").encode(), 0o600)
        mergeable.add(cursor_settings)
    return targets, mergeable, hook_groups


def _merged_hook_settings(
    outputs: dict[str, Path], settings: Path, managed_scripts: set[str],
    prior_hook_groups: dict[str, list[object]],
) -> tuple[bytes, dict[str, list[object]]]:
    fragment = json.loads(outputs["hooks/hooks.json"].read_text(encoding="utf-8"))
    prompt = outputs["hooks/turn-end-asks.md"].read_text(encoding="utf-8").rstrip("\n")
    fragment["hooks"]["Stop"][0]["hooks"][0]["prompt"] = prompt
    current = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    hooks = current.setdefault("hooks", {})
    for event, additions in fragment["hooks"].items():
        retained = []
        for group in hooks.get(event, []):
            commands = group.get("hooks", []) if isinstance(group, dict) else []
            owned = group in prior_hook_groups.get(event, []) or group in additions or any(
                PurePosixPath(str(hook.get("command", ""))).name in managed_scripts
                for hook in commands if isinstance(hook, dict)
            )
            if not owned:
                retained.append(group)
        hooks[event] = retained + additions
    if "statusLine" in fragment:
        current["statusLine"] = fragment["statusLine"]
    return (json.dumps(current, indent=2, sort_keys=True) + "\n").encode(), fragment["hooks"]


def _installed_document(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError("prompt install state is not a regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def _adoption_manifest(path: Path | None) -> dict[Path, tuple[str, str]]:
    if path is None or not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError("adoption manifest is not a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1:
        raise ValueError("adoption manifest version is unsupported")
    return {
        Path(target): (record["symlink_target"], record["sha256"])
        for target, record in value.get("targets", {}).items()
    }


def _adopted_directories(
    targets: dict[Path, tuple[bytes, int]], adoption: dict[Path, tuple[str, str]],
    home: Path, vscode_root: Path,
) -> set[Path]:
    adopted: set[Path] = set()
    roots = (home, vscode_root)
    for target in targets:
        for parent in target.parents:
            if parent in roots:
                break
            if not parent.is_symlink():
                continue
            declared = adoption.get(parent)
            if (declared is None or os.readlink(parent) != declared[0]
                    or _directory_digest(parent) != declared[1]):
                raise ValueError("legacy directory symlink does not match adoption manifest")
            adopted.add(parent)
            break
    return adopted


def _directory_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("legacy adoption directory contains an unsafe entry")
        if path.is_file():
            relative = path.relative_to(directory).as_posix().encode()
            content = path.read_bytes()
            digest.update(relative + b"\0" + content)
    return digest.hexdigest()


def _require_safe_parent(target: Path, home: Path, vscode_root: Path) -> None:
    if not target.is_absolute() or ".." in target.parts:
        raise ValueError("prompt consumer target escapes its root")
    _safe_directory(target.parent)
    roots = tuple(root.resolve() for root in (home, vscode_root))
    resolved_parent = target.parent.resolve()
    if not any(resolved_parent == root or root in resolved_parent.parents for root in roots):
        raise ValueError("prompt consumer target escapes its root")


def write_projection_manifest(
    repository: Path,
    projection_root: Path,
    installed_root: Path,
    generator_path: str,
) -> Path:
    """Bind an existing regular-file projection to the repository's current commit."""
    commit = _git(repository, "rev-parse", "HEAD^{commit}").strip()
    generator = _safe_path(generator_path)
    outputs = _compiled_outputs(projection_root)
    return _write_manifest(repository, commit, projection_root, installed_root, generator, outputs)


def _source_entries(repository: Path, commit: str) -> dict[str, tuple[int, str]]:
    result = _git_bytes(repository, "ls-tree", "-rz", "--full-tree", commit)
    entries: dict[str, tuple[int, str]] = {}
    folded: set[str] = set()
    for record in filter(None, result.split(b"\0")):
        metadata, raw_path = record.split(b"\t", 1)
        mode_text, kind, blob = metadata.decode("ascii").split()
        try:
            path = _safe_path(raw_path.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ValueError("unsafe committed source path") from error
        key = path.as_posix().casefold()
        if key in folded or kind != "blob" or mode_text not in {"100644", "100755"}:
            raise ValueError("unsafe committed source entry")
        folded.add(key)
        entries[path.as_posix()] = (int(mode_text[-3:], 8), blob)
    return entries


def _materialize(repository: Path, entries: dict[str, tuple[int, str]], root: Path) -> None:
    root.mkdir(mode=0o700)
    for value, (mode, blob) in entries.items():
        destination = root / value
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(_git_bytes(repository, "show", blob))
        destination.chmod(mode)


def _compiled_outputs(root: Path) -> dict[str, tuple[int, bytes]]:
    candidates = [root / name for name in _OUTPUT_FILES]
    for directory in (*_OUTPUT_ROOTS, *_PASSTHROUGH_ROOTS):
        location = root / directory
        if location.is_symlink():
            raise ValueError("compiled projection contains a non-regular output")
        if location.exists():
            candidates.extend(location.rglob("*"))
    outputs: dict[str, tuple[int, bytes]] = {}
    folded: set[str] = set()
    for path in sorted(candidates):
        if path.is_symlink() or (path.exists() and not path.is_file() and not path.is_dir()):
            raise ValueError("compiled projection contains a non-regular output")
        if not path.is_file():
            continue
        relative = _safe_path(path.relative_to(root).as_posix())
        key = relative.as_posix().casefold()
        if key in folded:
            raise ValueError("compiled projection has a case-fold collision")
        mode = PinnedDirectory.permissions(path.stat())
        if mode not in {0o600, 0o644, 0o755}:
            raise ValueError("compiled projection has an unsafe mode")
        folded.add(key)
        outputs[relative.as_posix()] = (mode, path.read_bytes())
    if not outputs:
        raise ValueError("prompt generator produced no projection")
    return outputs


def _normalize_output_modes(root: Path) -> None:
    """Remove ambient umask from generated regular-file projection modes."""
    candidates = [root / name for name in _OUTPUT_FILES]
    for directory in _OUTPUT_ROOTS:
        location = root / directory
        if location.exists():
            candidates.extend(location.rglob("*"))
    for path in candidates:
        if path.is_symlink():
            raise ValueError("compiled projection contains a non-regular output")
        if path.is_file():
            path.chmod(0o644)


def _copy_passthrough_outputs(root: Path, entries: dict[str, tuple[int, str]]) -> None:
    """Retain committed hook sources that are installed without generation."""
    for relative in entries:
        path = PurePosixPath(relative)
        if path.parts[0] not in _PASSTHROUGH_ROOTS:
            continue
        output = root / relative
        if output.is_symlink() or not output.is_file():
            raise ValueError("passthrough projection output is not a regular file")
        output.chmod(0o755 if output.suffix == ".sh" else 0o644)


def _write_manifest(
    repository: Path,
    commit: str,
    projection_root: Path,
    _installed_root: Path,
    generator: PurePosixPath,
    outputs: dict[str, tuple[int, bytes]],
) -> Path:
    tree = _git(repository, "rev-parse", f"{commit}^{{tree}}").strip()
    timestamp = _git(repository, "show", "-s", "--format=%cI", commit).strip()
    generator_blob = _git(repository, "rev-parse", f"{commit}:{generator.as_posix()}").strip()
    generator_bytes = _git_bytes(repository, "show", generator_blob)
    manifest = {
        "generator": {
            "blob": generator_blob,
            "path": generator.as_posix(),
            "sha256": hashlib.sha256(generator_bytes).hexdigest(),
        },
        "outputs": [
            {
                "consumers": _consumer_kinds(relative),
                "mode": f"{mode:04o}",
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for relative, (mode, content) in sorted(outputs.items())
        ],
        "source_commit": commit,
        "source_timestamp": timestamp,
        "source_tree": tree,
        "version": 1,
    }
    destination = projection_root / MANIFEST_NAME
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return destination


def _consumer_kinds(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if path.parts[0] == "references":
        return ("immutable-cache-reference",)
    if relative == "hooks/codex-hooks.json":
        return ("codex-settings-merge",)
    if relative == "hooks/cursor-hooks.json":
        return ("cursor-settings-merge",)
    if relative == "AGENTS.md":
        return ("home-agents", "codex-agents")
    if relative == "instructions.md":
        return ("claude-claude", "vscode-instructions")
    if relative == "org.json":
        return ("claude-org",)
    if len(path.parts) == 2 and path.parts[0] == "agents":
        return ("claude-agent", "vscode-agent")
    if len(path.parts) == 3 and path.parts[0] == "skills":
        return ("claude-skill", "codex-skill", "vscode-skill")
    if len(path.parts) == 2 and path.parts[0] == "rules":
        return ("claude-rule", "vscode-rule")
    if len(path.parts) == 2 and path.parts[0] == "hooks" and path.suffix == ".sh":
        return ("claude-hook",)
    if relative in {"hooks/hooks.json", "hooks/turn-end-asks.md"}:
        return ("claude-settings-merge",)
    if _scoped_output(path) is not None:
        return ("claude-scope",)
    raise ValueError("projection output has no consumer mapping")


def _scoped_output(path: PurePosixPath) -> str | None:
    """Return the scope name of a `scopes/<scope>/{agents/<a>.md,skills/<s>/SKILL.md}` output."""
    parts = path.parts
    if len(parts) == 4 and parts[0] == "scopes" and parts[2] in {"agents", "rules"} and path.suffix == ".md":
        return parts[1]
    if len(parts) == 5 and parts[0] == "scopes" and parts[2] == "skills" and path.name == "SKILL.md":
        return parts[1]
    return None


def _safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe repository path")
    return path


def _git(repository: Path, *arguments: str) -> str:
    return _git_bytes(repository, *arguments).decode("utf-8")


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, timeout=30
    )
    if result.returncode:
        raise ValueError("Git prompt source operation failed")
    return result.stdout
