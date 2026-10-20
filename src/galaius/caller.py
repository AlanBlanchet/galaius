"""Who a call is made for: the environment, folder and process of the client a request came from.

One `galaius mcp` server serves every MCP client of a computer (`galaius.server.shared`), so the
server's own environment, working folder and process say nothing about the agent calling: each
connection binds its client's (`Caller.bound`, from the relay's hello) and everything that reads a
caller's identity — its run, parent run, role, conversation, folder, the environment a child it
spawns inherits — reads `Caller.current()`. Outside any bound scope (a CLI command, a test) the
caller is this process itself."""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


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
    #: What names one caller or how it reaches the network: a process serving many callers holds none of
    #: these of its own (whichever client started it would otherwise speak for all), its callers' only.
    PERSONAL: ClassVar[tuple[str, ...]] = (
        "GALAIUS_RUN_ID", "GALAIUS_PARENT_RUN_ID", "GALAIUS_SESSION_ID", "GALAIUS_RUN_ROLE", "GALAIUS_STOP_BY",
        "GALAIUS_PROJECT_SETTINGS_OFF", "CLAUDE_PROJECT_DIR", *THREAD,
        "http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    )

    @classmethod
    def here(cls) -> "Caller":
        """This process as its own caller."""
        return cls(environment=dict(os.environ), cwd=Path.cwd(), pid=os.getpid())

    @classmethod
    def current(cls) -> "Caller":
        """The caller bound to this context, else this process."""
        return cls._current.get() or cls.here()

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
