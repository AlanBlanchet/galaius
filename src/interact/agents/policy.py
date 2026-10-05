"""How agents run here: profiles, toolsets, paradigms, and which providers are switched on.

    ~/.interact/agents.json
    {
      "profiles":  {"eyes": "cap.vlm and gui.screenspot > 0.85 and price.in < 10"},
      "agents":    {"visual-critic": "@eyes", "ux-critic": "@eyes"},
      "toolsets":  {"vision": ["screenshot", "review_ui"], "browse": ["navigate", "@vision"]},
      "agent_tools": {"visual-critic": ["@browse"]},
      "paradigms": {"tester": [{"paradigm": "coding", "as": "system_prompt"}]},
      "providers": {"claude": true, "codex": false}
    }

Four asks, one shape. A PROFILE is a model rule written once, worn by many agents; a TOOLSET is a
set of tools named once instead of spelling `mcp__interact__…` at every call site; a PARADIGM
assignment is instruction content written once and PROJECTED per agent, choosing whether that
agent carries it always (`system_prompt`) or loads it on demand (`skill`) — same content,
different altitude per agent; a PROVIDER switch says whether interact drives agents through that
vendor at all. Each is a NAME standing for something you'd otherwise repeat, so they share one
file and one grammar instead of four conventions to remember.

Everything is validated when the file is READ — unknown profile, toolset cycle, unknown
projection kind — because the alternative is discovering it at 3am when that agent spawns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from interact_core import AgentRevisionRef

from interact.agents.catalog import AgentCatalog
from interact.agents.catalog_connection import CatalogConnectionError
from interact.agents.vocabulary import TouchScope
from interact.config import UserConfig
from interact.criteria import ValueRule

#: The two altitudes a paradigm can be projected at for one agent. Closed on purpose: a third
#: spelling ("prompt", "sticky-note") is a typo, silent until spawn.
ParadigmProjection = Literal["skill", "system_prompt"]
_PROJECTIONS: tuple[ParadigmProjection, ...] = ("skill", "system_prompt")


@dataclass(frozen=True)
class ParadigmAssignment:
    """One paradigm, worn by one agent, at one altitude."""

    paradigm: str
    projection: ParadigmProjection


class PolicyError(ValueError):
    """A policy file that cannot be honoured. Raised at LOAD, naming what to fix."""


#: How vendor CLIs address an MCP server's tools. interact's own tools are the ones named here,
#: so the prefix is added rather than typed forty times.
TOOL_PREFIX = "mcp__interact__"


def policy_path() -> Path:
    """Where the policy lives: beside `config.env`, the one store every front end shares.

    NOT under the debug dir: a box that relocates its dumps (`INTERACT_DEBUG_DIR`) would drag the
    policy along — the CLI once looked for `<repo>/out/agents.json` while the panel wrote
    `~/.interact/agents.json`: one fact in two files, and a choice that never bit. The extension
    computes the same path on its own (`agentModels.ts`); `tests/test_paths.py` holds the two together.
    """
    return UserConfig.PATH.parent / "agents.json"


@dataclass
class Policy:
    profiles: dict[str, str] = field(default_factory=dict)
    agents: dict[str, str] = field(default_factory=dict)
    toolsets: dict[str, list[str]] = field(default_factory=dict)
    agent_tools: dict[str, list[str]] = field(default_factory=dict)
    paradigms: dict[str, list[ParadigmAssignment]] = field(default_factory=dict)
    providers: dict[str, bool] = field(default_factory=dict)
    #: Models switched off on this machine (`"claude/claude-fable-5-1": false`): never launched, not
    #: even as a fallback, whatever a role's criterion ranks. A local cost choice, like `providers`.
    models: dict[str, bool] = field(default_factory=dict)
    defaults: dict[str, str] = field(default_factory=dict)
    reasoning: dict[str, str] = field(default_factory=dict)
    #: Optional model-specific effort override. Keys use the standard `provider/model` id; this
    #: is applied after candidate selection, so one role can keep a cheaper model shallow and a
    #: stronger model deep without teaching the caller a vendor flag.
    reasoning_models: dict[str, str] = field(default_factory=dict)
    #: Per-agent providers explicitly ALLOWED to run it under COARSE (sandbox-scope, not per-tool)
    #: enforcement — a machine-local acceptance, same footing as `providers` (a role's own
    #: allowed/denied tool list stays catalog- or file-owned; this only says whether a provider
    #: that cannot express that list may run the role anyway, under its own sandbox instead).
    #: Never a default: a CLI that cannot enforce a role's tool list is refused unless the
    #: operator names this tradeoff here.
    coarse_tool_policy: dict[str, list[str]] = field(default_factory=dict)
    #: Intelligence against cost (`ValueRule`): `"value": {"ceiling": 1.5, "edge": 2}` — a model
    #: costing more than `ceiling` times another for less than `edge` points more is never run.
    #: A machine-local cost choice, like `models`; absent means the defaults. Prices are read at
    #: each role's own measured token mix, never a mix typed here.
    value: ValueRule = field(default_factory=ValueRule)
    catalog: AgentCatalog | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        """Read the policy, or an empty one. A missing file is not an error — most people never
        write one, and interact's defaults must work with nothing configured at all."""
        if path is None:
            try:
                catalog = AgentCatalog.active()
            except CatalogConnectionError as error:
                raise PolicyError(str(error)) from error
            if catalog is not None:
                return cls.from_catalog(catalog)
        path = Path(path) if path is not None else policy_path()
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as err:
            raise PolicyError(f"{path} is not readable policy: {err}") from err
        if not isinstance(raw, dict):
            raise PolicyError(f"{path} must hold an object")
        policy = cls(
            profiles=dict(raw.get("profiles") or {}),
            agents=dict(raw.get("agents") or {}),
            toolsets={k: list(v) for k, v in (raw.get("toolsets") or {}).items()},
            agent_tools={k: list(v) for k, v in (raw.get("agent_tools") or {}).items()},
            paradigms=_parse_paradigms(raw.get("paradigms") or {}, path),
            providers=dict(raw.get("providers") or {}),
            models=dict(raw.get("models") or {}),
            defaults=dict(raw.get("defaults") or {}),
            reasoning=dict(raw.get("reasoning") or {}),
            reasoning_models=dict(raw.get("reasoning_models") or {}),
            coarse_tool_policy={k: list(v) for k, v in (raw.get("coarse_tool_policy") or {}).items()},
            value=_value_rule(raw, path),
        )
        policy.validate()
        return policy

    @classmethod
    def from_catalog(cls, catalog: AgentCatalog) -> "Policy":
        policy = cls(catalog=catalog)
        # Provider installation switches are local machine preferences, not role policy.
        try:
            raw = json.loads(policy_path().read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as error:
            raise PolicyError("Cannot read local provider switches") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("providers", {}), dict):
            raise PolicyError("Local provider switches must be an object")
        policy.providers = dict(raw.get("providers", {}))
        if not isinstance(raw.get("models", {}), dict):
            raise PolicyError("Local model switches must be an object")
        policy.models = dict(raw.get("models", {}))
        if not isinstance(raw.get("defaults", {}), dict):
            raise PolicyError("Local agent defaults must be an object")
        policy.defaults = dict(raw.get("defaults", {}))
        if not isinstance(raw.get("coarse_tool_policy", {}), dict):
            raise PolicyError("Local coarse tool-policy acceptance must be an object")
        policy.coarse_tool_policy = {
            k: list(v) for k, v in (raw.get("coarse_tool_policy") or {}).items()
        }
        policy.value = _value_rule(raw, policy_path())
        for agent in catalog.launch_agents:
            if agent.role_key is None:
                continue
            if agent.criteria:
                policy.agents[agent.role_key] = agent.criteria
            elif agent.model is not None:
                policy.agents[agent.role_key] = agent.model.id
            policy.reasoning[agent.role_key] = agent.reasoning
            policy.agent_tools[agent.role_key] = list(agent.harness_tools)
            policy.paradigms[agent.role_key] = [
                ParadigmAssignment(paradigm=f"{reference.key.namespace}/{reference.key.slug}", projection=projection)
                for projection, references in (("system_prompt", agent.paradigms), ("skill", agent.skill_paradigms))
                for reference in references
            ]
        policy.validate()
        return policy

    def for_launch(
        self, role: str | None, *, reference: AgentRevisionRef | None = None,
        parent: AgentRevisionRef | None = None, capability: str | None = None,
    ) -> tuple["Policy", str]:
        """Resolve a declared delegation pin before reading any model or instructions."""
        if self.catalog is None:
            if reference is not None or parent is not None or capability is not None:
                raise PolicyError("Exact revision delegation requires a configured server catalog")
            if not role:
                raise PolicyError("Choose a named agent role; anonymous delegation bypasses role criteria")
            return self, role
        selected = reference
        if capability is not None and parent is None:
            raise PolicyError("A delegate capability requires a parent run with a recorded server revision")
        if parent is not None:
            parent_agent = self.catalog.at_revision(parent).revision(parent)
            delegates = [value for value in parent_agent.capabilities if value.kind == "delegate"]
            if capability is not None:
                matches = [value.agent for value in delegates if value.name == capability]
                if not matches:
                    raise PolicyError(f"Parent revision has no delegate capability {capability!r}")
            else:
                identity = reference.id if reference is not None else self.catalog.snapshot.role(role).id if role else None
                matches = list({value.agent for value in delegates if value.agent.id == identity})
            if len(matches) > 1:
                raise PolicyError("Parent has multiple revisions for this child; select a delegate capability")
            if matches:
                if reference is not None and reference != matches[0]:
                    raise PolicyError("Requested agent revision conflicts with the parent's delegation pin")
                selected = matches[0]
        if selected is not None:
            catalog = self.catalog.at_revision(selected)
            agent = catalog.revision(selected)
            if agent.role_key is None:
                raise PolicyError("Pinned agent revision has no launch role key")
            if role is not None and role != agent.role_key:
                raise PolicyError("Requested role does not match the pinned agent revision")
            return self.from_catalog(catalog), agent.role_key
        if not role:
            raise PolicyError("Choose a named role or an exact agent revision")
        self.catalog.snapshot.role(role)
        return self, role

    def validate(self) -> None:
        """Everything that can be wrong, found now rather than at spawn."""
        touch = self.defaults.get("touch")
        if touch is not None:
            try:
                TouchScope(touch)
            except ValueError as error:
                raise PolicyError(
                    f"default touch intent {touch!r} is unsupported; choose one of "
                    f"{', '.join(scope.value for scope in TouchScope)}"
                ) from error
        if self.defaults.get("model"):
            self.rule(self.defaults["model"], wearer="default")
        efforts = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
        for agent, effort in {"default": self.defaults.get("reasoning", "medium"),
                              **self.reasoning}.items():
            if effort not in efforts:
                raise PolicyError(f"agent {agent!r} has unsupported reasoning {effort!r}")
        for model, effort in self.reasoning_models.items():
            if effort not in efforts:
                raise PolicyError(f"model {model!r} has unsupported reasoning {effort!r}")
        for agent, rule in self.agents.items():
            self.rule(rule, wearer=agent)  # raises on a profile that does not exist
        for name in self.toolsets:
            self.expand_toolset(name)  # raises on a cycle or an unknown reference
        # The projection kind is validated by `_parse_paradigms` at load, ahead of here — content
        # existence is a separate, offline-unavailable check (`agents.paradigms.plan_projections`).

    def default_touch_intent(self) -> str | None:
        """The machine-local touch intent applied when a caller leaves autonomy unspecified."""
        value = self.defaults.get("touch")
        if value is None:
            return None
        return TouchScope(value).value

    @staticmethod
    def _looks_like_profile(rule: str) -> bool:
        """`@name` references a profile — the same sigil a toolset uses to reference another.

        A sigil, not a heuristic: `ghost-profile` and `claude-sonnet-5` are the same SHAPE, so
        guessing would either mistake a typo'd profile for a model id (discovering it at 3am) or
        refuse a legitimate id. One character removes the whole question.
        """
        return rule.startswith("@")

    def criterion_for(self, agent: str) -> str | None:
        """What model rule this agent runs under: its profile's criterion, its own inline
        criterion, or a plain model id — whichever the file says. None when unmentioned."""
        if self.catalog is not None:
            self.catalog.role(agent)
        rule = self.agents.get(agent, self.defaults.get("model"))
        return None if rule is None else self.rule(rule, wearer=agent)

    def weights_for(self, agent: str) -> str:
        return "" if self.catalog is None else self.catalog.role(agent).criteria_weights

    def reasoning_for(self, agent: str, model: str | None = None) -> str:
        """Explicit role effort, independent of the parent session's effort."""
        if model is not None:
            model_keys = [model]
            if "/" in model:
                model_keys.append(model.split("/", 1)[1])
            for key in model_keys:
                if key in self.reasoning_models:
                    return self.reasoning_models[key]
        if self.catalog is not None:
            return self.catalog.role(agent).reasoning
        return self.reasoning.get(agent, self.defaults.get("reasoning", "medium"))

    def rule(self, text: str, *, wearer: str | None = None) -> str:
        """`@name` → that profile's criterion; anything else is returned as written.

        A profile is a NAME for a model rule, so it is honoured wherever a model may be named —
        the `agents` map here, `--model @eyes` on the CLI, the panel's picker. An unknown one is
        refused naming the typo and what exists; `wearer` is the agent wearing it, when there is one.
        """
        if not self._looks_like_profile(text):
            return text
        if text[1:] in self.profiles:
            return self.profiles[text[1:]]
        known = ", ".join(sorted(self.profiles)) or "none defined"
        if wearer:
            raise PolicyError(f"agent {wearer!r} wears profile {text!r}, which does not exist (have: {known})")
        raise PolicyError(f"profile {text!r} does not exist (have: {known})")

    def expand_toolset(self, name: str, _seen: tuple[str, ...] = ()) -> list[str]:
        """A toolset's tools, fully prefixed, with `@other-set` references expanded in place."""
        if name in _seen:
            raise PolicyError(f"toolset cycle: {' -> '.join([*_seen, name])}")
        members = self.toolsets.get(name)
        if members is None:
            raise PolicyError(f"toolset {name!r} does not exist")
        out: list[str] = []
        for member in members:
            out.extend(self._expand_member(member, (*_seen, name)))
        return _dedupe(out)

    def _expand_member(self, member: str, seen: tuple[str, ...]) -> list[str]:
        if member.startswith("@"):
            return self.expand_toolset(member[1:], seen)
        # Already addressed in full (another server's tool, or somebody being explicit) — left as
        # written: guessing a prefix for somebody else's server is worse.
        if member.startswith("mcp__") or "__" in member:
            return [member]
        return [f"{TOOL_PREFIX}{member}"]

    def tools_for(self, agent: str) -> list[str]:
        """The tools this agent may use, or [] when the policy does not restrict it."""
        if self.catalog is not None:
            return list(self.catalog.role(agent).harness_tools)
        members = self.agent_tools.get(agent)
        if not members:
            return []
        out: list[str] = []
        for member in members:
            out.extend(self._expand_member(member, ()))
        return _dedupe(out)

    def provider_active(self, provider: str) -> bool:
        """Whether interact drives agents through this vendor.

        Unmentioned means ON: a provider whose CLI is installed and whose key is set is one the
        person meant to use, and a policy file nobody wrote must never silently disable anything.
        """
        return bool(self.providers.get(provider, True))

    def model_active(self, provider: str, model: str, catalog_id: str | None = None) -> bool:
        """Whether this model may run here, read under any of its names (`claude/<id>`, the
        catalog's `anthropic/<id>`, the bare id). Unmentioned means ON, like `provider_active`."""
        names = (f"{provider}/{model}", model, *((catalog_id,) if catalog_id else ()))
        return all(bool(self.models.get(name, True)) for name in names)

    def accepts_coarse_tool_policy(self, agent: str, provider: str) -> bool:
        """Whether THIS role explicitly accepted running on a provider that can only enforce a
        SANDBOX (not this role's specific allowed/denied tool names) — named once per role, per
        provider, in `coarse_tool_policy`. Unmentioned means NO: the opposite default of
        `provider_active`, because here silence must never widen what a role can reach."""
        return provider in self.coarse_tool_policy.get(agent, [])

    def set_provider_active(self, provider: str, active: bool, path: Path | None = None) -> None:
        """Flip a provider on or off and write it back, so the CLI and the panel's toggle are
        reading and writing the same one fact."""
        path = Path(path) if path is not None else policy_path()
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as err:
            # The file holds profiles and toolsets somebody typed. One missing comma must not let
            # a provider switch flatten it: refuse, name the file, write nothing.
            raise PolicyError(f"{path} is not readable policy ({err}); fix it — nothing was written") from err
        if not isinstance(raw, dict):
            raise PolicyError(f"{path} must hold an object — nothing was written")
        raw.setdefault("providers", {})[provider] = bool(active)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2) + "\n")
        self.providers[provider] = bool(active)

    def assign_paradigm(
        self, agent: str, paradigm: str, projection: ParadigmProjection, path: Path | None = None,
    ) -> None:
        """Give `agent` a paradigm at one altitude — SAME read-modify-write shape as
        `set_provider_active`, so a choice made anywhere reads back identically everywhere.

        Re-assigning a paradigm already on this agent REPLACES its projection in place rather
        than appending a second, contradictory entry; a new paradigm is appended, order kept —
        assignment order is the order a system prompt's fragments are concatenated in.
        """
        if self.catalog is not None:
            raise PolicyError("Server catalog is configured; edit the agent's paradigms on the server, then sync")
        if projection not in _PROJECTIONS:
            raise PolicyError(
                f"{projection!r} is not a paradigm projection — write one of {_PROJECTIONS}"
            )
        path = Path(path) if path is not None else policy_path()
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as err:
            raise PolicyError(f"{path} is not readable policy ({err}); fix it — nothing was written") from err
        if not isinstance(raw, dict):
            raise PolicyError(f"{path} must hold an object — nothing was written")
        entries = raw.setdefault("paradigms", {}).setdefault(agent, [])
        entry = {"paradigm": paradigm, "as": projection}
        for existing in entries:
            if existing.get("paradigm") == paradigm:
                existing["as"] = projection
                break
        else:
            entries.append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2) + "\n")
        self.paradigms[agent] = [
            ParadigmAssignment(paradigm=e["paradigm"], projection=e["as"]) for e in entries
        ]


def _value_rule(raw: dict, path: Path) -> ValueRule:
    """The file's `value` block as a `ValueRule`, refused at load when it cannot be one."""
    try:
        return ValueRule.model_validate(raw.get("value") or {})
    except ValueError as error:
        raise PolicyError(f"{path}: `value` must look like {{\"ceiling\": 1.5, \"edge\": 2}} ({error})") from error

def _parse_paradigms(
    raw: dict[str, list[dict[str, str]]], path: Path,
) -> dict[str, list[ParadigmAssignment]]:
    """Turn the raw `paradigms` object into typed assignments, refusing an unknown projection
    kind HERE — at load — rather than at the spawn that silently gets no instructions at all."""
    out: dict[str, list[ParadigmAssignment]] = {}
    for agent, entries in raw.items():
        assignments = []
        for entry in entries:
            projection = entry.get("as")
            if projection not in _PROJECTIONS:
                raise PolicyError(
                    f"{path}: {agent!r} is assigned paradigm {entry.get('paradigm')!r} "
                    f"as {projection!r} — write one of {_PROJECTIONS}"
                )
            assignments.append(ParadigmAssignment(paradigm=entry["paradigm"], projection=projection))
        out[agent] = assignments
    return out


def _dedupe(names: list[str]) -> list[str]:
    """Order preserved — the file's order is the reader's order, and a set would scramble it."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out
