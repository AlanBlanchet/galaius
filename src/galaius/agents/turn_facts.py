"""What a coordinator's card states as read at the turn the person's words start (`Config.agent_turn_facts`): the build
the app runs, his PCs as the server sees them (online, last seen, what stops one, its work folders), and this PC's own
state (whether it runs agents, its agent folders and what agents may do there, each program's login). Each is read now,
within the budget; one not read in time, or refused, says so, never a remembered value."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from galaius.agents.providers import PROVIDERS
from galaius.config.settings import TurnFacts

#: A build tag's leading time (`20261022T134400Z-e079498`), stamped when its deploy started.
STAMPED = re.compile(r"^(\d{8}T\d{6})Z")

Reader = Callable[[], str]


class Readers(BaseModel):
    """What the card reads: the build, his PCs, this PC's agent settings, each program's login here."""

    model_config = ConfigDict(frozen=True)

    build: Reader
    pcs: Reader
    this_pc: Reader
    logins: dict[str, Reader] = Field(default_factory=dict)


def build() -> str:
    """The build the app runs, with when its tag was stamped."""
    from galaius.server_workspace import ServerWorkspace  # Circular layers: server_workspace imports the agents package.

    tag = ServerWorkspace.configured().version().build_id or "unknown"
    at = STAMPED.match(tag)
    return f"{tag} (tag stamped {datetime.strptime(at.group(1), '%Y%m%dT%H%M%S').replace(tzinfo=UTC):%Y-%m-%d %H:%M} UTC)" if at else tag


def pcs() -> str:
    """His PCs as the server sees them now, one line each (other PCs' logins are not in what it reports)."""
    from galaius.account_login import AccountLogin  # Circular layers: account_login imports machines, which imports the agents package.
    from galaius.machines import MachineRunner

    machine = MachineRunner().load()
    lines = []
    for pc in AccountLogin.at(machine.server_url).machines(machine):
        if pc.state == "revoked":
            continue
        seen = f", last seen {pc.last_seen_at:%Y-%m-%d %H:%M} UTC" if pc.last_seen_at else ", never seen"
        problem = f", stopped by: {pc.problem.code}" if pc.problem is not None else ""
        folders = f", work folders: {', '.join(pc.file_roots) or 'none'}" if pc.file_roots is not None else ""
        here = " (this PC)" if pc.id == machine.machine_id else ", logins not reported"
        lines.append(f"- {pc.name}{here}: {pc.state}{seen}{problem}{folders}")
    return "\n".join(lines) or "none"


def this_pc() -> str:
    """Whether this PC runs agents, in which folders, and what they may do there (its machine settings)."""
    from galaius.machines import MachineRunner  # Circular layers: machines imports the agents package.

    config = MachineRunner().load()
    if not config.run_agents:
        return "runs no agents"
    return f"agent folders {', '.join(config.agent_roots) or 'none'}; agents may: {config.agent_permission}"


def login(name: str) -> str:
    """Program `name`'s default login here, as its CLI says now."""
    status = asyncio.run(PROVIDERS[name].login_status(dict(os.environ)))
    return "signed in" if status.signed_in else "signed out" if status.signed_in is False else "status unknown"


def readers() -> Readers:
    return Readers(build=build, pcs=pcs, this_pc=this_pc, logins={name: (lambda name=name: login(name)) for name in PROVIDERS})


def lines(facts: TurnFacts, *, read: Readers | None = None) -> str:
    """The card's lines, every reader (`readers`) run at once within `facts.seconds`."""
    if not facts.enabled:
        return ""
    read = read or readers()
    tasks: dict[str, Reader] = {"build": read.build, "pcs": read.pcs, "this_pc": read.this_pc, **{f"login:{name}": reader for name, reader in read.logins.items()}}
    pool = ThreadPoolExecutor(max_workers=len(tasks))
    futures: dict[str, Future] = {key: pool.submit(reader) for key, reader in tasks.items()}
    wait(futures.values(), timeout=facts.seconds)
    pool.shutdown(wait=False, cancel_futures=True)
    said = {key: _said(future, facts.seconds) for key, future in futures.items()}
    logins = ", ".join(f"{name} {said[f'login:{name}']}" for name in read.logins) or "none"
    return "\n".join((
        f"Read at this turn ({time.strftime('%H:%M:%S')}): app build {said['build']}.",
        f"His PCs, as the server sees them:\n{said['pcs']}",
        f"This PC ({socket.gethostname()}): {said['this_pc']}; logins: {logins}.",
    ))


def _said(future: Future, seconds: float) -> str:
    """What one reader read, or why nothing was: never an older value."""
    if not future.done():
        return f"not read (no answer within {seconds:g} s)"
    if (error := future.exception()) is not None:
        return f"not read ({str(error)[:160] or type(error).__name__})"
    return future.result()
