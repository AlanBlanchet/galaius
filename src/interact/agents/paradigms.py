"""A paradigm: instruction content authored once, projected per-agent as a skill or a system
prompt.

"In interact, we should have these paradigms! Such that later on a user doesn't edit a 'system
prompt' or a 'skill', but a paradigm, and choses to add it to an agent as 'skill' or as 'system
prompt'." The choice is per AGENT, not per paradigm: the same paradigm can be a lazily-loaded
skill for one agent (loaded on demand, cheap on context) and baked straight into another agent's
always-present system prompt — because those two agents need the same knowledge at different
altitudes.

A paradigm file is plain markdown with a small frontmatter, the same shape as an agent definition
or a skill file: `name`, `description`, then body text. Reading it requires nothing beyond a file
on disk — no network, no compiled projection — so `plan_projections` runs offline against whatever
prompt repository the caller points it at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from interact.agents.policy import Policy


class ParadigmError(ValueError):
    """A paradigm that cannot be read or resolved. Raised naming the paradigm, never silently."""


_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_FIELD = re.compile(r'^(name|description):\s*"?(.*?)"?\s*$', re.MULTILINE)


@dataclass(frozen=True)
class ParadigmContent:
    """One paradigm file's content, parsed. `body` is what a projection actually carries — the
    frontmatter is metadata about it, never part of the instruction text itself."""

    name: str
    description: str
    body: str


def read_paradigm(name: str, repository: Path) -> ParadigmContent:
    """Read one paradigm by name from `repository/paradigms/<name>.md`.

    Raises :class:`ParadigmError` naming the paradigm when the file is missing — a paradigm a
    policy references but the repository does not have is a 3am spawn failure waiting to happen,
    not something to discover quietly.
    """
    path = repository / "paradigms" / f"{name}.md"
    try:
        raw = path.read_text()
    except FileNotFoundError as err:
        raise ParadigmError(f"paradigm {name!r} not found at {path}") from err
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise ParadigmError(f"paradigm {name!r} has no --- frontmatter to read a name/description from")
    front, body = match.groups()
    fields = dict(_FIELD.findall(front))
    return ParadigmContent(
        name=fields.get("name", name), description=fields.get("description", ""),
        body=body.strip(),
    )


@dataclass(frozen=True)
class SkillOutput:
    """One paradigm projected as a skill for one agent — a file that agent loads on demand rather
    than carries in its system prompt on every turn."""

    agent: str
    name: str
    description: str
    body: str


@dataclass
class ProjectionPlan:
    """Every paradigm assignment in a policy, resolved into what it actually produces.

    Two buckets because the two projections are consumed differently: a skill is one FILE per
    agent per paradigm (discoverable, loaded on demand); a system-prompt fragment is TEXT appended
    into that agent's always-present instructions, in assignment order.
    """

    skills: list[SkillOutput] = field(default_factory=list)
    system_prompt_fragments: dict[str, list[str]] = field(default_factory=dict)


def plan_projections(policy: Policy, repository: Path) -> ProjectionPlan:
    """Resolve every `paradigms` assignment in `policy` against `repository`.

    Raises :class:`ParadigmError` on the FIRST assignment naming a paradigm the repository does
    not have — never drops one silently and produces a plan short by one agent's instructions.
    """
    plan = ProjectionPlan()
    for agent, assignments in policy.paradigms.items():
        for assignment in assignments:
            content = read_paradigm(assignment.paradigm, repository)
            if assignment.projection == "skill":
                plan.skills.append(SkillOutput(
                    agent=agent, name=content.name, description=content.description,
                    body=content.body,
                ))
            else:
                plan.system_prompt_fragments.setdefault(agent, []).append(content.body)
    return plan
