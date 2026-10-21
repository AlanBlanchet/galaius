"""Who a call is made for: the environment, folder and process of the client a request came from.

One `galaius mcp` server serves every MCP client of a computer (`galaius.server.shared`), so the
server's own environment, working folder and process say nothing about the agent calling: each
connection binds its client's (`Caller.bound`, from the relay's hello) and everything that reads a
caller's identity — its run, parent run, role, conversation, folder, the environment a child it
spawns inherits — reads `Caller.current()`. Outside any bound scope (a CLI command, a test) the
caller is this process itself.

A process inside a run's own scope (`processes.unit_run`) acts as that run whatever its environment says: `env -u`, a
cleared parent or `setsid` leave a process in the scope it was started in, so the run variables (`RUN_VARIABLES`) are
taken from the scope, never from the variables. A process `systemd-run` handed to the user manager is in no run's scope
and is no one (`Caller.unowned`)."""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from galaius.processes import process_unit, started_by_systemd_run, unit_run

#: The variables naming the run a process acts as (its launch sets both to the run's id).
RUN_VARIABLES = ("GALAIUS_RUN_ID", "GALAIUS_PARENT_RUN_ID")


class Caller(BaseModel):
    """One client's environment, working folder and process id, as its relay announced them."""

    model_config = ConfigDict(frozen=True)
    environment: Mapping[str, str] = Field(default_factory=dict)
    cwd: Path
    pid: int
    _current: ClassVar[ContextVar["Caller | None"]] = ContextVar("galaius_caller", default=None)
    #: Where each agent CLI tells a command it runs which conversation it is in: a run launched from
    #: that shell (a script, a background command) belongs to that conversation, so the reader can show
    #: the prompt it answered (runs launched by a script from Claude Code recorded no owner at all).
    THREAD: ClassVar[tuple[str, ...]] = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")
    #: This process serves many callers (`galaius.server.shared`): it never stands in for one.
    _serving: ClassVar[bool] = False

    @model_validator(mode="before")
    @classmethod
    def _scoped(cls, data: dict) -> dict:
        """Its run variables from the run scope its process lives in (`processes.unit_run`), when it lives in one."""
        if not isinstance(data, dict) or (run := cls.scope_run(int(data.get("pid", 0) or 0))) is None:
            return data
        return {**data, "environment": {**dict(data.get("environment") or {}), **dict.fromkeys(RUN_VARIABLES, run)}}

    @staticmethod
    def scope_run(pid: int) -> str | None:
        """The run whose own scope process `pid` lives in; None outside every run's scope (the person's terminal, the
        app's services, a system without user systemd)."""
        return unit_run(process_unit(pid))

    def unowned(self) -> bool:
        """Whether its process is one `systemd-run` started (`processes.started_by_systemd_run`): it may act as no run and
        is not the person either (2026-10-21: an agent sent a message as the person from such a unit)."""
        return started_by_systemd_run(process_unit(self.pid))

    @classmethod
    def here(cls) -> "Caller":
        """This process as its own caller."""
        return cls(environment=dict(os.environ), cwd=Path.cwd(), pid=os.getpid())

    @classmethod
    def current(cls) -> "Caller":
        """The caller bound to this context, else this process; LookupError in a process serving many
        callers (`serve_many`), where an unbound read would answer for whichever client started it."""
        if (bound := cls._current.get()) is not None:
            return bound
        if cls._serving:
            raise LookupError("no caller is bound here: this server answers for its clients, never for itself")
        return cls.here()

    @classmethod
    def serve_many(cls) -> None:
        """From now on this process serves many callers: every read of a caller needs one bound."""
        cls._serving = True

    @classmethod
    def environ(cls, environment: Mapping[str, str] | None = None) -> Mapping[str, str]:
        """`environment` when a caller passed one, else the current caller's."""
        return cls.current().environment if environment is None else environment

    @contextmanager
    def bound(self) -> Iterator["Caller"]:
        token = self._current.set(self)
        try:
            yield self
        finally:
            self._current.reset(token)

    def path(self, value: str | Path) -> Path:
        """`value` as the caller means it: a relative path lies in its working folder."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.cwd / path
