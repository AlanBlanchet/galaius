"""Validated server catalog, replaceable local cache, and role prompt composition."""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import ClassVar, Self
from uuid import UUID

import httpx
from galaius_core import AgentCatalogSnapshot, AgentRevision, AgentRevisionRef, PromptExecutionRef, PromptRevision
from pydantic import BaseModel, ConfigDict, Field, model_validator

from galaius.agents.catalog_connection import (
    CatalogAuthenticationError,
    CatalogConnection,
    CatalogConnectionError,
    CatalogOffline,
)
from galaius.criteria import Criteria
from galaius.private_files import PRIVATE_FILES


def role_identifier(agent: AgentRevision) -> str:
    """The name a consumer knows a role by (agent file, delegate label): its role key, else its id."""
    return agent.role_key or f"agent-{agent.id}"


def skill_file(prompt: PromptRevision) -> PurePosixPath:
    """Where a skill revision lives below any skill root. PromptKey and digest are core-validated
    path components; the digest keeps earlier launches' references intact when a new catalog lands."""
    return PurePosixPath(prompt.key.namespace, prompt.key.slug, prompt.digest, "SKILL.md")


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

    def narrowed(self) -> Self:
        """This set carrying only the instruction revisions its own agent references: a head snapshot hands over every
        role's prompts, so a copy kept per revision (`AgentCatalog.pin_path`) would keep the whole catalog once per
        agent."""
        agent = self.agent
        wanted = dict.fromkeys((agent.prompt, *agent.paradigms, *agent.skill_paradigms))
        return type(self)(agent=agent, prompts=tuple(self.prompt(reference) for reference in wanted))

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

    def delegated(self, name: str) -> AgentRevisionRef:
        """The revision a delegate capability of this name launches, wherever this catalog declares it.

        Capabilities are generated from `reports_to`, so only a LEAD revision carries any: a builder
        commissioning its critic (`delegate=ask_visual_critic`) has none of its own and was refused
        outright, as was every child whose own record this computer cannot read (2026-10-15). The name
        resolves to the PIN the catalog declares for it, never to a role name read out of the tool's
        own name — that would launch a revision nobody pinned."""
        pins = {capability.agent for agent in self.agents for capability in agent.capabilities
                if capability.kind == "delegate" and capability.name == name}
        if not pins:
            raise CatalogConnectionError(f"the active server catalog declares no delegate capability {name!r}")
        if len(pins) == 1:
            return next(iter(pins))
        held = [pin for pin in pins if any(agent.id == pin.id and agent.revision == pin.revision for agent in self.agents)]
        if len(held) != 1:
            raise CatalogConnectionError(
                f"delegate capability {name!r} is declared at {len(pins)} revisions in this catalog; "
                "launch that agent by its exact revision")
        return held[0]

    def prompt(self, reference: PromptExecutionRef) -> PromptRevision:
        return AgentInstructionSet.find_prompt(self.paradigms, reference)

    def definition(self, key: str, skill_root: PurePath | None,
                   instructions: AgentInstructionSet | None = None, *,
                   described_roles: frozenset[str] = frozenset()) -> str:
        """Role instructions as the model reads them on every call: no identifier it never passes.

        `skill_root`: the folder holding each skill at `skill_file(prompt)` (None: no skill is listed).
        `described_roles`: roles the consumer's own agent list already describes (Claude Code's
        Agent tool lists every installed agent); their delegate lines name the role only."""
        source = instructions or AgentInstructionSet(agent=self.role(key), prompts=self.paradigms)
        agent = source.agent
        fragments = [source.instruction_body(source.prompt(reference)) for reference in (agent.prompt, *agent.paradigms)]
        if agent.skill_paradigms and skill_root is not None:
            fragments.append("\n".join((f"Available skills (read {skill_root}/<skill path> only when its skill is needed):",
                                         *(f"- {skill_file(source.prompt(reference))}" for reference in agent.skill_paradigms))))
        peers = {value.id: value for value in self.agents}
        team = [f"Role: {key}; name: {agent.name}; department: {agent.department or 'unassigned'}; scope: {agent.scope}"]
        if agent.reports_to is not None:
            lead = peers.get(agent.reports_to)
            label = f"{lead.name} (role: {lead.role_key or 'unassigned'})" if lead else f"{agent.reports_to} (active role unavailable)"
            team.append(f"Reports to: {label}")
        delegates = [capability for capability in agent.capabilities if capability.kind == "delegate"]
        if delegates:
            # The parent run's recorded revision resolves each name to its exact pin (agent_spawn):
            # the identity and revision ids never need to be in the text the model re-reads.
            team.append("Delegates (agent_spawn delegate=<name> launches that role at its pinned revision):")
        for capability in delegates:
            target = peers.get(capability.agent.id)
            label = (
                f"{target.name} (role: {target.role_key or 'unassigned'})"
                if target is not None else "active role unavailable"
            )
            described = target is not None and role_identifier(target) in described_roles
            team.append(f"- {capability.name}: {label}" + ("" if described else f". {capability.description}"))
        for capability in agent.capabilities:
            if capability.kind != "delegate":
                team.append(f"Server capability {capability.name} ({capability.kind}): {capability.description}")
        fragments.append("Team and capabilities:\n" + "\n".join(team))
        return "\n\n".join(fragments)


#: How many revisions of ONE agent this computer keeps as pins (`AgentCatalog.pin_path`), the most recently read first:
#: a run and its queued messages ask for the revision they were launched at, never for a whole history of them.
PINS_PER_AGENT = 8
#: How the tool lists of a cached catalog read: 2 since galaius-core 0.58 (`AgentRevision.harness_tools` [] = no tool, a
#: left-out list = every tool). A cache file with no mark was written when [] meant every tool: never read again.
TOOL_LISTS = 2


class CachedRecord(BaseModel):
    """What this computer keeps of a server answer in one private file, to work from while the service cannot be
    reached: read back only when this galaius reads its content the way it was written (`TOOL_LISTS`), bounded by the
    size a catalog answer may have, and tagged with the connection and access it was granted under — a caller
    compares both before using it, so a revoked access is never worked around by a file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection: CatalogConnection
    fetched_at: datetime
    access_generation: UUID = UUID(int=0)
    #: `TOOL_LISTS` when written; a file without it predates it (`written_now`).
    tool_lists: int = TOOL_LISTS
    #: What one of these files may weigh: the catalog response limit (`CatalogConnection._request_once`).
    LIMIT: ClassVar[int] = 16 * 1024 * 1024

    @staticmethod
    def written_now(payload: bytes) -> bool:
        """A file whose tool lists read as this galaius reads them (`TOOL_LISTS`)."""
        try:
            return json.loads(payload).get("tool_lists") == TOOL_LISTS
        except (ValueError, AttributeError):
            return False

    @classmethod
    def cached(cls, target: Path) -> Self | None:
        """What was last written to `target`, None when there is none this galaius reads."""
        try:
            with target.open("rb") as source:
                payload = source.read(cls.LIMIT + 1)
            if len(payload) > cls.LIMIT:
                return None
            return cls.model_validate_json(payload) if cls.written_now(payload) else None
        except (OSError, ValueError):
            return None

    def usable_by(self, connection: CatalogConnection, generation: UUID) -> bool:
        """This file may be worked from by a reader on `connection` holding access `generation`."""
        return self.connection == connection and self.access_generation == generation


class PinnedRevision(CachedRecord):
    """One exact agent revision as the server answered for it, kept for launching and messaging it again while the
    service cannot be reached (`AgentCatalog.pin_path`). Its `AgentInstructionSet` validates every instruction
    reference again on read, so a truncated or edited file is no launch."""

    instructions: AgentInstructionSet


class AgentCatalog(CachedRecord):
    """One connection-bound snapshot; refresh before use, never merge with local authoring."""

    snapshot: CatalogSnapshot
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
    def pins_path(cls) -> Path:
        """Where every exact revision this computer has been answered for is kept (`pin_path`), beside the head
        cache: the same access is granted for all of them, so one sweep drops them together."""
        return cls.cache_path().parent / "agent-catalog-pins"

    @classmethod
    def pin_path(cls, reference: AgentRevisionRef) -> Path:
        """Where the one exact revision `reference` names is kept once the server has answered for it
        (`at_revision`): its own file, in the same validated form as the head cache — a snapshot of that agent alone
        with the prompts it references, so reading it back validates every reference again and costs no new format.
        Its own file per revision: a pin resolved for one launch is still there for the next, and a catalog that moved
        on never overwrites it."""
        # Both are server-issued UUIDs, so neither can name a path component of its own.
        return cls.pins_path() / str(reference.id) / f"{reference.revision}.json"

    def pinned_offline(self, reference: AgentRevisionRef) -> Self | None:
        """This catalog with the last revision `reference` the server answered for as its selection, read from
        `pin_path`; None when this computer holds none it may use (`CachedRecord.usable_by`). The pin itself, never
        the latest in its place: what was launched before is what is launched now, which is the whole point of a
        pin."""
        held = PinnedRevision.cached(self.pin_path(reference))
        if held is None or not held.usable_by(self.connection, self.access_generation):
            return None
        agent = held.instructions.agent
        if agent.id != reference.id or agent.revision != reference.revision:
            return None
        with self.connection.access_guard(self.access_generation):
            return self.model_copy(update={"selection": held.instructions, "stale": True})

    def remember_pin(self, selection: AgentInstructionSet) -> None:
        """Keep `selection` as answered, for `pinned_offline` to work from while the service cannot be reached — every
        resolution of a pin, the one the head catalog already held included: a revision current at a run's start is a
        historical one by the time a message reaches that run, which is where 9 queued messages were dropped during
        one catalog outage (2026-10-23). Tagged with the connection this catalog was read through, the one `pinned_offline`
        compares against: the authenticated copy of it is never the tag, since a failure before authentication is
        exactly when the pin is read back."""
        instructions = selection.narrowed()
        reference = AgentRevisionRef(id=selection.agent.id, revision=selection.agent.revision)
        target = self.pin_path(reference)
        held = PinnedRevision.cached(target)
        if held is not None and held.usable_by(self.connection, self.access_generation) and (
                held.instructions == instructions):
            target.touch()  # read again, so keeping the newest `PINS_PER_AGENT` keeps the ones in use
            return
        value = PinnedRevision(connection=self.connection, instructions=instructions, fetched_at=datetime.now(UTC),
                               access_generation=self.access_generation)
        with self.connection.access_guard(self.access_generation):
            PRIVATE_FILES.replace_text(target, value.model_dump_json())
            # One agent's history is unbounded; what a run or a message still asks for is what was read most recently.
            for old in sorted(target.parent.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[PINS_PER_AGENT:]:
                old.unlink(missing_ok=True)

    @classmethod
    def discard_invalidated_cache(cls, connection: CatalogConnection, target: Path) -> None:
        """Every snapshot this computer may no longer read is dropped: the head cache at `target` and the exact
        revisions remembered beside it (`pin_path`), which the same access was granted for."""
        with connection.access_guard() as generation:
            pins = cls.pins_path()
            for path in sorted(pins.glob("*/*.json")) if pins.is_dir() else ():
                pin = PinnedRevision.cached(path)
                if pin is None or (pin.connection == connection and pin.access_generation != generation):
                    path.unlink(missing_ok=True)
            try:
                payload = target.read_bytes()
                cached = cls.model_validate_json(payload)
            except FileNotFoundError:
                return
            except ValueError:
                target.unlink(missing_ok=True)
                return
            if not cls.written_now(payload) or (cached.connection == connection and cached.access_generation != generation):
                target.unlink(missing_ok=True)

    @classmethod
    def refresh(
        cls, connection: CatalogConnection, *, allow_stale: bool = False,
        cache_path: Path | None = None, transport: httpx.BaseTransport | None = None,
        known: Self | None = None,
    ) -> Self:
        """The server's current catalog. Asked conditionally on the snapshot already held
        (`known`, else the cache file): its cursor is the entity tag the server answers 304 to
        while nothing changed, so an unchanged catalog costs no download."""
        target = cache_path if cache_path is not None else cls.cache_path()
        generation = connection.access_generation()
        resolved = connection
        try:
            with connection.connect(transport=transport) as client:
                resolved = connection.authenticate(client)
                if resolved != connection:
                    generation = resolved.access_generation()
                held = known if known is not None else cls.cached(target)
                if held is not None and (held.selection is not None or held.connection != resolved or held.access_generation != generation):
                    held = None
                payload = resolved.request_if_changed(
                    client, f"/v1/workspaces/{resolved.workspace_id}/agent-catalog",
                    held.snapshot.cursor if held is not None else None,
                )
            if payload is None:
                assert held is not None  # a 304 answers only the tag sent
                with resolved.access_guard(generation):
                    return held.model_copy(update={"stale": False})
            try:
                snapshot = CatalogSnapshot.model_validate_json(payload)
            except ValueError as error:
                raise CatalogConnectionError("invalid server agent catalog; cache was not replaced") from error
            value = cls(connection=resolved, snapshot=snapshot, fetched_at=datetime.now(UTC), access_generation=generation)
            with resolved.access_guard(generation):
                PRIVATE_FILES.replace_text(target, value.model_dump_json())
                return value
        except CatalogAuthenticationError:
            cls.discard_invalidated_cache(resolved, target)
            # A key this PC was given can be revoked while its own link still works: the agents it
            # starts read the catalog through that link instead (no key to make, no step).
            linked = CatalogConnection.linked() if connection.auth_mode == "token" else None
            if linked is None or linked.endpoint != connection.endpoint.rstrip("/"):
                raise
            return cls.refresh(linked, allow_stale=allow_stale, cache_path=cache_path, transport=transport)
        except (CatalogOffline, httpx.TransportError) as error:  # unreachable, dropped mid-answer, or a front with no app (5xx)
            if not allow_stale:
                raise CatalogConnectionError("agent catalog service is unreachable; sync was not applied") from error
            with connection.access_guard(generation):
                cached = cls.cached(target)
                if cached is None or cached.selection is not None:
                    raise CatalogConnectionError("agent catalog is unreachable and no validated cache is available") from error
                if cached.connection != connection:
                    raise CatalogConnectionError("cached agent catalog belongs to a different connection") from error
                if cached.access_generation != generation:
                    raise CatalogAuthenticationError("cached catalog access was invalidated; sync online before use")
                print(f"STALE agent catalog: service unreachable; using snapshot fetched {cached.fetched_at.isoformat()}", file=sys.stderr)
                return cached.model_copy(update={"stale": True})

    @classmethod
    def active(cls) -> Self | None:
        """The catalog this process launches from: read at most once per
        `CatalogConnection.REUSE_SECONDS`, each read a conditional one."""
        connection = CatalogConnection.load()
        if connection is None:
            return None
        return connection.recent("agent-catalog", lambda known: cls.refresh(connection, allow_stale=True, known=known))

    def at_revision(self, reference: AgentRevisionRef) -> Self:
        with self.connection.access_guard(self.access_generation):
            if self.selection is not None and self.selection.agent.id == reference.id and self.selection.agent.revision == reference.revision:
                return self
            held = next((agent for agent in self.snapshot.agents
                         if agent.id == reference.id and agent.revision == reference.revision), None)
        if held is not None:
            selection = AgentInstructionSet(agent=held, prompts=self.snapshot.paradigms)
            self.remember_pin(selection)
            return self.model_copy(update={"selection": selection})
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
            self.remember_pin(selection)
            with self.connection.access_guard(self.access_generation):
                return self.model_copy(update={"selection": selection})
        except CatalogAuthenticationError:
            self.discard_invalidated_cache(self.connection, self.cache_path())
            raise
        except (CatalogOffline, httpx.TransportError) as error:
            # Unreachable, timed out, dropped mid-answer, or a front with no app behind it (502 / 503 / 504 on every deploy
            # cutover: `SERVICE_DOWN`, code-reviewer r1 B1) — the service is down JUST NOW.
            # Retried already (`CatalogConnection.RETRY_BACKOFF`), so the service is down for longer than a blip. THIS
            # revision as this computer last read it still launches it; the latest in its place never would.
            if (held := self.pinned_offline(reference)) is not None:
                print(f"STALE agent revision: service unreachable; using the pin as last read "
                      f"({reference.id} {reference.revision})", file=sys.stderr)
                return held
            # A pin this computer never read: nothing to start it from yet. Raised as `CatalogOffline`, so his message is kept
            # and waits instead of being refused (`messaging.queue_message`, the dispatcher's `SERVICE_WAIT_SECONDS`).
            raise CatalogOffline("pinned agent revision is unavailable during network failure; latest revision is not a fallback") from error
        except CatalogConnectionError:
            raise
        except ValueError as error:
            raise CatalogConnectionError("invalid exact agent or prompt revision received from server") from error

    def role_prompt(self, role: str, *, loaded_from: Path | None = None) -> str:
        """Complete pinned role instructions, independent of a delegated task.

        `loaded_from`: the user instructions file the CLI reads at start when it already holds
        this role's body (see `prompt_projection.coordinator_instructions`); only the header is
        sent then, the body once instead of twice."""
        agent = self.role(role)
        # Only what names the content: this text heads the system prompt, so a byte that changes
        # between starts (when it was fetched, whether the server answered) makes the vendor
        # write the whole prompt to its cache again instead of reading it.
        header = (
            f"AGENT_ROLE: {role}\n\n"
            f"Server agent identity: {agent.id}; revision: {agent.revision}\n\n"
            f"Head catalog: cursor={self.snapshot.cursor}\n\n"
        )
        if loaded_from is not None:
            return f"{header}Role instructions: {loaded_from}, loaded at start."
        return header + self.instructions(role)

    def definition(self, role: str, task: str) -> str:
        return f"{self.role_prompt(role)}\n\nDelegated task:\n{task}"

    def instructions(self, role: str) -> str:
        agent = self.role(role)
        source = self.selection or AgentInstructionSet(agent=agent, prompts=self.snapshot.paradigms)
        for reference in agent.skill_paradigms:
            self.skill_path(reference)
        return self.snapshot.definition(role, self.skill_root(), instructions=source)

    def skill_root(self) -> Path:
        return self.cache_path().parent / "agent-catalog-skills"

    def skill_path(self, reference: PromptExecutionRef) -> Path:
        prompt = self.prompt(reference)
        target = self.skill_root() / skill_file(prompt)
        PRIVATE_FILES.replace_text(target, prompt.content)
        return target

    def definition_path(self, role: str) -> Path:
        # role() resolves a core-validated kebab-case key before it becomes a filename.
        agent = self.role(role)
        target = self.cache_path().parent / "agent-catalog-definitions" / str(agent.id) / str(agent.revision) / f"{agent.role_key}.md"
        PRIVATE_FILES.replace_text(target, self.instructions(role))
        return target
