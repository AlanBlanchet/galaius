"""A slice that passed its critic and sits committed on no remote is not delivered. A maker closing « DONE » on it strands it: the
critic's PASS came back, nobody pushed or enqueued it, and a perf-reviewed fix sat unshipped for a day (2026-11-22: a close said
« Fix committed … not pushed yet. Perf-critic … Its verdict comes back to this run, then I push », and its verdict's hand-back
failed). `refusal` says why such a close may not stand; `galaius.close_hook` runs it at the end of the turn, through
`galaius.agents.turns.running_claim`, only when the closing run's HEAD is on no remote (`close_hook.unpushed`).

The evidence is the run records and git, never the maker's words: a child of the closing run ended after the HEAD commit was made
and opened its report with a verdict word (`VERDICTS`), and no remote branch holds HEAD. Delivery stays the project's (a push, a
queue): the refusal names no tool."""

import re
from datetime import datetime

from galaius.agents import registry as reg
from galaius.agents.catalog import relations
from galaius.close_hook import says_done, unpushed

#: The words a critic opens its report with when its verdict lets the slice in.
VERDICTS = re.compile(r"^\W*(?:PASS|ACCEPTED)\b", re.IGNORECASE)


def _verdict(child: reg.AgentRun) -> bool:
    return any(VERDICTS.match(part) for part in relations.final_parts(child.run_id)[:1])


def refusal(run_id: str, text: str) -> str | None:
    """Why the close `text` of run `run_id` may not stand, None when it may: it says DONE while its folder's HEAD, made during this
    run, is on no remote and a child of this run passed it after it was made."""
    run = reg.get_run(run_id)
    if run is None or not run.cwd or not says_done(text):
        return None
    held = unpushed(run.cwd)
    if held is None or held[1] < run.started_at:
        return None
    head, made = held
    passed = [child for child in reg.children_of(run_id) if child.finished_at is not None and child.finished_at > made and _verdict(child)]
    if not passed:
        return None
    stamp = datetime.fromtimestamp(made).strftime("%m-%d %H:%M")
    judged = ", ".join(f"{child.agent or child.name} {reg.short_id(child.run_id)}" for child in passed)
    return (f"DELIVERY CHECK (galaius runtime): this close says DONE, but commit {head[:10]} ({stamp}) in {run.cwd} is on no remote and "
            f"{judged} passed it after it was made: a slice that passed its critic and was neither pushed nor enqueued is not delivered. "
            "Ship it as the project's delivery says (push, enqueue, wait for it), or close PARTIAL naming what holds it.")
