"""Validated server catalog, replaceable local cache, and role prompt composition."""

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import UUID

import httpx
from interact_core import AgentCatalogSnapshot, AgentRevision, AgentRevisionRef, PromptExecutionRef, PromptRevision
from pydantic import BaseModel, ConfigDict, Field, model_validator

from interact.agents.catalog_connection import (
    CatalogAuthenticationError,
    CatalogConnection,
    CatalogConnectionError,
)
from interact.criteria import Criteria


class AgentInstructionSet(BaseModel):
    """One exact launch revision and its complete verified instruction references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: AgentRevision
    prompts: tuple[PromptRevision, ...]

    @staticmethod
    def instruction_body(prompt: PromptRevision) -> str:
        """Frontmatter describes a source file; only its body becomes instructions."""
        content = prompt.content.removeprefix("\ufeff").replace("\r\n", "\n")
        if re.match(r"\A---[ \t]*\n", content):
            match = re.match(r"\A---[ \t]*\n(?:.*?\n)?---[ \t]*(?:\n|\Z)", content, re.DOTALL)
            if match is None:
                raise CatalogConnectionError("catalog instruction has unterminated frontmatter")
            content = content[match.end():]
        return content.strip()

    @staticmethod
    def find_prompt(prompts: tuple[PromptRevision, ...], reference: PromptExecutionRef) -> PromptRevision:
        for value in prompts:
            if value.key == reference.key and value.revision == reference.revision and value.digest == reference.digest:
                return value
        raise CatalogConnectionError("agent has an unresolved exact prompt revision")

    def prompt(self, reference: PromptExecutionRef) -> PromptRevision:
        return self.find_prompt(self.prompts, reference)

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.agent.criteria is not None:
            Criteria.parse(self.agent.criteria)
        Criteria.validate_weights(self.agent.criteria_weights)
        for reference in (self.agent.prompt, *self.agent.paradigms, *self.agent.skill_paradigms):
            self.prompt(reference)
        for reference in (self.agent.prompt, *self.agent.paradigms):
            self.instruction_body(self.prompt(reference))
        return self


class CatalogSnapshot(AgentCatalogSnapshot):
    """Core's verified wire snapshot with local launch accessors."""

    @model_validator(mode="after")
    def verify_snapshot(self) -> Self:
        if len(self.agents) > 1_000 or len(self.paradigms) > 20_000:
            raise ValueError("agent catalog exceeds its record limit")
        for agent in self.agents:
            if agent.criteria is not None:
                Criteria.parse(agent.criteria)
            Criteria.validate_weights(agent.criteria_weights)
        return self

    def role(self, key: str) -> AgentRevision:
        for agent in self.agents:
            if agent.role_key == key:
                return agent
        raise CatalogConnectionError(f"role {key!r} is absent from the active server catalog")

    def revision(self, reference: AgentRevisionRef) -> AgentRevision:
        for agent in self.agents:
            if agent.id == reference.id and agent.revision == reference.revision:
                return agent
        raise CatalogConnectionError("pinned agent revision is absent from this catalog")

    def prompt(self, reference: PromptExecutionRef) -> PromptRevision:
        return AgentInstructionSet.find_prompt(self.paradigms, reference)

    def definition(self, key: str, skill_paths: dict[PromptExecutionRef, Path],
                   instructions: AgentInstructionSet | None = None) -> str:
        source = instructions or AgentInstructionSet(agent=self.role(key), prompts=self.paradigms)
        agent = source.agent
        fragments = [source.instruction_body(source.prompt(reference)) for reference in (agent.prompt, *agent.paradigms)]
        if agent.skill_paradigms:
            fragments.append("Available skills (read the referenced file only when its skill is needed):")
            for reference in agent.skill_paradigms:
                value = source.prompt(reference)
                fragments.append(f"- Skill: {value.key.namespace}/{value.key.slug}; file: {skill_paths[reference]}")
        peers = {value.id: value for value in self.agents}
        team = [f"Role: {key}; name: {agent.name}; department: {agent.department or 'unassigned'}; scope: {agent.scope}"]
        if agent.reports_to is not None:
            lead = peers.get(agent.reports_to)
            label = f"{lead.name} (role: {lead.role_key or 'unassigned'})" if lead else f"{agent.reports_to} (active role unavailable)"
            team.append(f"Reports to: {label}")
        for capability in agent.capabilities:
            if capability.kind == "delegate":
                target = peers.get(capability.agent.id)
                label = (
                    f"{target.name} (role: {target.role_key or 'unassigned'})"
                    if target is not None else "active role unavailable"
                )
                team.append(
                    f"Delegate capability {capability.name}: {label}. {capability.description} "
                    f"Server agent identity: {capability.agent.id}. "
                    f"Server capability revision: {capability.agent.revision}. "
                    f"Invoke agent_spawn with delegate set to {capability.name} to use this pin."
                )
            else:
                team.append(f"Server capability {capability.name} ({capability.kind}): {capability.description}")
        fragments.append("Team and capabilities:\n" + "\n".join(team))
        return "\n\n".join(fragments)


class AgentCatalog(BaseModel):
    """One connection-bound snapshot; refresh before use, never merge with local authoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection: CatalogConnection
    snapshot: CatalogSnapshot
    fetched_at: datetime
    access_generation: UUID = UUID(int=0)
    stale: bool = Field(default=False, exclude=True)
    selection: AgentInstructionSet | None = Field(default=None, exclude=True)

    @property
    def launch_agents(self) -> tuple[AgentRevision, ...]:
        return (self.selection.agent,) if self.selection is not None else self.snapshot.agents

    def role(self, key: str) -> AgentRevision:
        if self.selection is None:
            return self.snapshot.role(key)
        if self.selection.agent.role_key != key:
            raise CatalogConnectionError("requested role differs from the selected exact revision")
        return self.selection.agent

    def revision(self, reference: AgentRevisionRef) -> AgentRevision:
        if self.selection is not None and self.selection.agent.id == reference.id and self.selection.agent.revision == reference.revision:
            return self.selection.agent
        return self.snapshot.revision(reference)

    def prompt(self, reference: PromptExecutionRef) -> PromptRevision:
        return self.snapshot.prompt(reference) if self.selection is None else self.selection.prompt(reference)

    @staticmethod
    def reference(agent_id: UUID | None, revision: UUID | None) -> AgentRevisionRef | None:
        if agent_id is None and revision is None:
            return None
        if agent_id is None or revision is None:
            raise CatalogConnectionError("Exact launch requires both --agent-id and --agent-revision")
        return AgentRevisionRef(id=agent_id, revision=revision)

    @classmethod
    def cache_path(cls) -> Path:
        return CatalogConnection.path().with_name("agent-catalog-cache.json")

    @classmethod
    def discard_invalidated_cache(cls, connection: CatalogConnection, target: Path) -> None:
        with connection.access_guard() as generation:
            try:
                cached = cls.model_validate_json(target.read_bytes())
            except FileNotFoundError:
                return
            except ValueError:
                target.unlink(missing_ok=True)
                return
            if cached.connection == connection and cached.access_generation != generation:
                target.unlink(missing_ok=True)

    @classmethod
    def refresh(
        cls, connection: CatalogConnection, *, allow_stale: bool = False,
        cache_path: Path | None = None, transport: httpx.BaseTransport | None = None,
    ) -> Self:
        target = cache_path if cache_path is not None else cls.cache_path()
        generation = connection.access_generation()
        resolved = connection
        try:
            with connection.connect(transport=transport) as client:
                resolved = connection.authenticate(client)
                if resolved != connection:
                    generation = resolved.access_generation()
                payload = resolved.request(client, "GET", f"/v1/workspaces/{resolved.workspace_id}/agent-catalog")
            try:
                snapshot = CatalogSnapshot.model_validate_json(payload)
            except ValueError as error:
                raise CatalogConnectionError("invalid server agent catalog; cache was not replaced") from error
            value = cls(connection=resolved, snapshot=snapshot, fetched_at=datetime.now(UTC), access_generation=generation)
            with resolved.access_guard(generation):
                CatalogConnection.replace_text(target, value.model_dump_json())
                return value
        except CatalogAuthenticationError:
            cls.discard_invalidated_cache(resolved, target)
            raise
        except (httpx.NetworkError, httpx.TimeoutException) as error:
            if not allow_stale:
                raise CatalogConnectionError("agent catalog service is unreachable; sync was not applied") from error
            with connection.access_guard(generation):
                try:
                    with target.open("rb") as source:
                        payload = source.read(16 * 1024 * 1024 + 1)
                    if len(payload) > 16 * 1024 * 1024:
                        raise ValueError("oversized cache")
                    cached = cls.model_validate_json(payload)
                    if cached.selection is not None:
                        raise ValueError("active cache contains a launch selection")
                except (OSError, ValueError) as cache_error:
                    raise CatalogConnectionError("agent catalog is unreachable and no validated cache is available") from cache_error
                if cached.connection != connection:
                    raise CatalogConnectionError("cached agent catalog belongs to a different connection") from error
                if cached.access_generation != generation:
                    raise CatalogAuthenticationError("cached catalog access was invalidated; sync online before use")
                print(f"STALE agent catalog: service unreachable; using snapshot fetched {cached.fetched_at.isoformat()}", file=sys.stderr)
                return cached.model_copy(update={"stale": True})

    @classmethod
    def active(cls) -> Self | None:
        connection = CatalogConnection.load()
        return None if connection is None else cls.refresh(connection, allow_stale=True)

    def at_revision(self, reference: AgentRevisionRef) -> Self:
        with self.connection.access_guard(self.access_generation):
            if self.selection is not None and self.selection.agent.id == reference.id and self.selection.agent.revision == reference.revision:
                return self
            for agent in self.snapshot.agents:
                if agent.id == reference.id and agent.revision == reference.revision:
                    return self.model_copy(update={"selection": AgentInstructionSet(agent=agent, prompts=self.snapshot.paradigms)})
        try:
            with self.connection.connect() as client:
                connection = self.connection.authenticate(client)
                base = f"/v1/workspaces/{connection.workspace_id}"
                agent = AgentRevision.model_validate_json(connection.request(
                    client, "GET", f"{base}/agents/{reference.id}/revisions/{reference.revision}",
                ))
                if agent.id != reference.id or agent.revision != reference.revision:
                    raise CatalogConnectionError("server returned a different agent revision than requested")
                prompts = []
                for ref in dict.fromkeys((agent.prompt, *agent.paradigms, *agent.skill_paradigms)):
                    try:
                        prompt = self.snapshot.prompt(ref)
                    except CatalogConnectionError:
                        prompt = PromptRevision.model_validate_json(connection.request(
                            client, "GET", f"{base}/prompts/{ref.key.namespace}/{ref.key.slug}?digest={ref.digest}",
                        ))
                        AgentInstructionSet.find_prompt((prompt,), ref)
                    prompts.append(prompt)
            selection = AgentInstructionSet(agent=agent, prompts=tuple(prompts))
            with self.connection.access_guard(self.access_generation):
                return self.model_copy(update={"selection": selection})
        except CatalogAuthenticationError:
            self.discard_invalidated_cache(self.connection, self.cache_path())
            raise
        except CatalogConnectionError:
            raise
        except (httpx.NetworkError, httpx.TimeoutException) as error:
            raise CatalogConnectionError("pinned agent revision is unavailable during network failure; latest revision is not a fallback") from error
        except ValueError as error:
            raise CatalogConnectionError("invalid exact agent or prompt revision received from server") from error

    def role_prompt(self, role: str) -> str:
        """Complete pinned role instructions, independent of a delegated task."""
        status = "stale" if self.stale else "current"
        agent = self.role(role)
        return (
            f"AGENT_ROLE: {role}\n\n"
            f"Server agent identity: {agent.id}; revision: {agent.revision}\n\n"
            f"Head catalog: {status}; cursor={self.snapshot.cursor}; fetched={self.fetched_at.isoformat()}\n\n"
            f"{self.instructions(role)}"
        )

    def definition(self, role: str, task: str) -> str:
        return f"{self.role_prompt(role)}\n\nDelegated task:\n{task}"

    def instructions(self, role: str) -> str:
        agent = self.role(role)
        source = self.selection or AgentInstructionSet(agent=agent, prompts=self.snapshot.paradigms)
        skill_paths = {reference: self.skill_path(reference) for reference in agent.skill_paradigms}
        return self.snapshot.definition(role, skill_paths, instructions=source)

    def skill_path(self, reference: PromptExecutionRef) -> Path:
        prompt = self.prompt(reference)
        # PromptKey and digest are core-validated filesystem components. Digest paths keep
        # earlier launches' lazy references intact when a new catalog becomes active.
        target = (
            self.cache_path().parent / "agent-catalog-skills" / prompt.key.namespace
            / prompt.key.slug / prompt.digest / "SKILL.md"
        )
        CatalogConnection.replace_text(target, prompt.content)
        return target

    def definition_path(self, role: str) -> Path:
        # role() resolves a core-validated kebab-case key before it becomes a filename.
        agent = self.role(role)
        target = self.cache_path().parent / "agent-catalog-definitions" / str(agent.id) / str(agent.revision) / f"{agent.role_key}.md"
        CatalogConnection.replace_text(target, self.instructions(role))
        return target
