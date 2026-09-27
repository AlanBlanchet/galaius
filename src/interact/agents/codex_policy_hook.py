"""Resolve native Codex delegation through the same policy the agent UI edits.

Hook failures deny launches. No inference, credentials, or transcript reads are needed.
Codex must trust the hook; disabled/untrusted hooks cannot enforce this policy.
"""

import json
import math
import os
from pathlib import Path
import re
import sys

from interact.agents.policy import Policy, PolicyError
from interact.agents.catalog import AgentCatalog
from interact.agents import registry as reg
from interact.agents.run import is_criterion
from interact.criteria import Criteria


_ROLE = re.compile(r"^AGENT_ROLE:\s*([a-z][a-z0-9_-]{0,79})\s*$", re.MULTILINE)
_LIMIT = 2 * 1024 * 1024


def _prompts() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "interact/prompts"


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        pieces = text.split("\n---", 1)
        if len(pieces) != 2:
            raise PolicyError(f"Invalid prompt frontmatter: {path.name}")
        return pieces[1].strip()
    return text.strip()


def _brief(arguments: dict) -> tuple[str, str, int | None]:
    """Support native message/prompt and structured-item tool versions."""
    for key in ("message", "prompt"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value, key, None
    for index, item in enumerate(arguments.get("items") or []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and _ROLE.search(text):
                return text, "items", index
    raise PolicyError("Start the delegation brief with AGENT_ROLE: <configured-role>.")


def _role(arguments: dict) -> tuple[str, str, str, int | None]:
    brief, key, index = _brief(arguments)
    match = _ROLE.match(brief.strip())
    if not match:
        raise PolicyError("Start the delegation brief with AGENT_ROLE: <configured-role>.")
    return match.group(1), brief, key, index


def _definition(role: str) -> str:
    root = _prompts()
    candidates = [root / "agents" / f"{role}.md"]
    candidates.extend(sorted(root.glob(f"scopes/*/agents/{role}.md")))
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise PolicyError(f"Role {role!r} needs one generated prompt definition; found {len(found)}.")
    return _body(found[0])


def _model_id(model) -> str:
    return model.id.removeprefix("openai/").removeprefix("chatgpt/")


def _select(policy: Policy, role: str) -> tuple[str, str, str, str]:
    rule = policy.criterion_for(role)
    if not rule:
        raise PolicyError(f"No criterion for {role!r}; configure it in the agent UI.")
    effort = policy.reasoning_for(role)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    cache = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    models = {
        row["slug"]: row for row in cache.get("models", [])
        if isinstance(row, dict) and row.get("slug") and row.get("visibility") == "list"
        and effort in {level.get("effort") for level in row.get("supported_reasoning_levels", [])}
    }
    if not models:
        raise PolicyError(f"Codex's model cache has no visible model supporting {effort!r} reasoning.")
    if not is_criterion(rule):
        model = rule.removeprefix("openai/").removeprefix("chatgpt/")
        if model not in models:
            raise PolicyError(
                f"{role!r} is pinned to {rule!r}, unavailable to native Codex. "
                "Use the configured Interact provider; do not substitute another model."
            )
        return model, effort, rule, "explicit model pin"

    def eligible(model) -> bool:
        prices = (model.input_cost_per_million, model.output_cost_per_million)
        return (
            model.provider in {"openai", "chatgpt"} and _model_id(model) in models
            and all(value is not None and math.isfinite(value) and value >= 0 for value in prices)
        )

    chosen = Criteria.parse(rule).choose(runnable=eligible, weights=policy.weights_for(role))
    if chosen is None:
        raise PolicyError(
            f"No native Codex model with known prices clears {role!r}: {rule!r} "
            f"at {effort} reasoning. Update benchmark/catalog data or edit the policy; "
            "no parent-model fallback was allowed."
        )
    price = (
        f"API reference ${chosen.input_cost_per_million:g}/${chosen.output_cost_per_million:g} "
        "per 1M input/output tokens, not subscription percentages"
    )
    return _model_id(chosen), effort, rule, price


def handle(event: dict) -> dict:
    kind = event.get("hook_event_name")
    if kind in {"SessionStart", "SubagentStart"}:
        catalog = AgentCatalog.active()
        if catalog is not None:
            context = (
                "Use AGENT_ROLE with a role from the configured server catalog for delegation. "
                "First progress message: [role-name] followed by the concrete task. "
                f"Catalog cursor: {catalog.snapshot.cursor}."
            )
            return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": context}}
    if kind == "SessionStart":
        return {"hookSpecificOutput": {
            "hookEventName": kind,
            "additionalContext": _body(_prompts() / "paradigms/provider-openai.md"),
        }}
    if kind == "SubagentStart":
        source = _body(_prompts() / "paradigms/expert.md")
        announcement = next(
            line for line in source.splitlines() if line.startswith("- First progress message:")
        )
        return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": announcement}}
    if kind not in {"PreToolUse", "PostToolUse"}:
        return {}
    arguments = event.get("tool_input")
    if not isinstance(arguments, dict):
        raise PolicyError("Native delegation arguments must be an object.")
    role, brief, key, index = _role(arguments)
    if kind == "PostToolUse":
        response = event.get("tool_response")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                return {}
        if isinstance(response, dict) and response.get("agent_id"):
            line = f"[{role}] started: thread {response['agent_id']}"
            return {"systemMessage": line, "hookSpecificOutput": {
                "hookEventName": kind,
                "additionalContext": line + ". Relay role and task in parent chat, then continue useful work.",
            }}
        return {}

    if arguments.get("agent_type") not in {None, "default", "worker", "explorer"}:
        raise PolicyError("Use a default native agent with AGENT_ROLE; custom config layers can override model policy.")
    policy = Policy.load()
    parent_id = os.environ.get("INTERACT_PARENT_RUN_ID")
    parent_run = reg.get_run(parent_id) if parent_id else None
    if policy.catalog is not None and parent_id and (parent_run is None or parent_run.agent_ref is None):
        raise PolicyError("Parent run has no recorded server revision; start the parent again")
    policy, role = policy.for_launch(role, parent=parent_run.agent_ref if parent_run is not None else None)
    if not policy.provider_active("codex"):
        raise PolicyError("Codex is disabled in the shared agent policy.")
    model, effort, rule, price = _select(policy, role)
    definition = _definition(role) if policy.catalog is None else policy.catalog.definition(role, "")
    updated = dict(arguments)
    updated["model"] = model
    updated["reasoning_effort"] = effort
    if "fork_context" in updated:
        updated["fork_context"] = False
    task = _ROLE.sub("", brief, count=1).strip()
    if not task:
        raise PolicyError("Delegation needs a concrete task after AGENT_ROLE.")
    text = (
        f"AGENT_ROLE: {role}\n\n"
        f"Launch metadata: role={role}; model={model}; reasoning={effort}; criterion={rule}.\n\n"
        f"Role instructions:\n{definition}\n\nDelegated task:\n{task}"
    )
    if index is None:
        updated[key] = text
    else:
        updated["items"] = [dict(item) for item in arguments["items"]]
        updated["items"][index]["text"] = text
    label = " ".join(task.split())[:160]
    line = f"[{role}] launch: {label} | {model}/{effort} | {rule} | {price}"
    return {"systemMessage": line, "hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "updatedInput": updated, "additionalContext": line,
    }}


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(_LIMIT + 1)
        if len(payload) > _LIMIT:
            raise PolicyError("Agent hook input exceeds its size limit.")
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise PolicyError("Agent hook event must be an object.")
        result = handle(event)
    except Exception as error:
        # Hook infrastructure treats other failures as advisory; exit 2 explicitly denies.
        print(f"Agent policy refused the operation: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
