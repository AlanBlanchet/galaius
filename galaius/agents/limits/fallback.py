"""A step of the model walk (`agents.walk`) that leaves a conversation's program: the table's first open unit is another program's,
and a session is its CLI's own format, so the conversation continues as a SUCCESSOR run there (`SpawnLine.hand_on`: the ranked walk
across programs, `relations.succeed`), carrying the message that was waiting and where it can be read; and the fresh run that takes
over a resume a bug of this computer's own code broke (`relaunch_fresh`).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from galaius.account.caller import RUN_VARIABLES, Caller
from galaius.agents import registry as reg

log = logging.getLogger(__name__)

#: How long the spawn of a successor may take to be handed over (`galaius agents spawn` returns once its run is in line).
SPAWN_SECONDS = 120


def _successor(run: reg.AgentRun, task: str, relaunches: reg.Relaunch | None) -> str | None:
    """A successor of `run` started as `SpawnLine.hand_on` starts it, `task` its brief; its run id, None when it could not be
    started (logged). The run it asks as is the stopped run's, never whichever run's environment this process was started from."""
    from galaius.agents.spawn_line import SpawnLine   # noqa: PLC0415 — circular: spawn_line imports run, which imports this package
    from galaius.agents.run import run_agent   # noqa: PLC0415 — circular, as above

    environment = {name: value for name, value in os.environ.items() if name not in RUN_VARIABLES}
    caller = Caller.current().model_copy(update={"environment": environment})

    def start() -> str:
        with caller.bound():
            return asyncio.run(SpawnLine.here().hand_on(run, task, functools.partial(run_agent, environment=environment), relaunches=relaunches))

    pool = ThreadPoolExecutor(max_workers=1)   # a thread of its own: its caller may run inside an event loop
    try:
        return pool.submit(start).result(timeout=SPAWN_SECONDS)
    except Exception as error:   # noqa: BLE001 — a successor that cannot start leaves the turn to its caller
        log.warning("could not hand run %s on to a successor: %s", run.run_id[:8], error)
        return None
    finally:
        pool.shutdown(wait=False)   # a start past `SPAWN_SECONDS` is left to end on its own, never waited for


def hand_on(run: reg.AgentRun, message: str) -> str | None:
    """Stopped `run`'s conversation continued by a successor on another provider (`SpawnLine.hand_on`: the ranked walk across
    providers, its own passed over by its notes), `message` its task with where the conversation can be read. The successor's run
    id, None when it could not be started (logged). Never called under `run`'s record lock: `relations.succeed` takes it."""
    return _successor(run, f"{message}\n\n[Galaius: you continue run {run.run_id[:8]} ({run.name}), which ran on {run.provider} and could not take this "
                           f"message: no model of its provider can take it on any login. Its conversation: `galaius agents events {run.run_id}`.]", None)


#: The errors that are this computer's own code failing, never a cause waiting changes: a resume that raises one is not tried again.
CODE_FAULTS = (AttributeError, TypeError, NameError, LookupError, AssertionError, ImportError)
#: How much of the ended run's last report its fresh successor is handed.
REPORT_CHARS = 4000


def code_fault(error: BaseException) -> bool:
    """Whether `error` is a bug of this computer's code (`CODE_FAULTS`): a resume it broke is repaired by a fresh run, at once."""
    return isinstance(error, CODE_FAULTS)


def relaunch_fresh(run: reg.AgentRun, message: str, error: BaseException | str) -> str | None:
    """Ended `run` could not be resumed for a code reason (`code_fault`: 2026-11-22 a resume read a field its model has not, and four
    critic reports never reached their makers): a FRESH run of its role, in its place, carries `message` (the one it could not take)
    and `run`'s last report; each records the other (`relaunched_from`, `replaced_by`), what is sent to `run` later reaches it
    (`relations.succeed`). The repair never depends on the path that broke: it starts a run, resumes nothing. Its run id, None when
    it could not be started (logged). Never called under `run`'s record lock."""
    from galaius.agents.catalog import relations   # noqa: PLC0415 — circular, as `_successor`

    why = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error
    last = "\n\n".join(relations.final_parts(run.run_id))[-REPORT_CHARS:]
    task = (f"{message}\n\n[Galaius: you continue run {run.run_id[:8]} ({run.name}), which could not be resumed ({why[:300]}). "
            f"Its conversation: `galaius agents events {run.run_id}`." + (f" Its last report:\n{last}" if last else "") + "]")
    return _successor(run, task, reg.Relaunch(run_id=run.run_id, provider=run.provider, model=run.model, said=why[:reg.RELAUNCH_SAID_MAX], cause="fault"))
