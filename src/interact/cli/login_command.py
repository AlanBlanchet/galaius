"""`interact login` / `interact logout`: this computer joins or leaves an Interact account."""

import sys
from typing import Annotated

from cyclopts import Parameter

from interact.account_login import LoginError, login as sign_in, logout as sign_out


def login(
    server: Annotated[str | None, Parameter(name="--server")] = None,
    *,
    allow_runs: bool = False,
    yes: bool = False,
    browser: bool = True,
    agents: bool | None = None,
    agent_folder: Annotated[tuple[str, ...], Parameter(name="--agent-folder")] = (),
) -> None:
    """Connect this computer to your Interact account: approve it in the browser, then it runs as
    one of your machines (Linux user service), with your agents and prompts synced. --server defaults to
    the server you installed from or last signed in to, else it is asked once;
    --allow-runs lets this CLI start workflow runs (default: read only); --yes skips the final
    question; --no-browser only prints the page to open. Right after the approval it asks once
    whether agents may run here and in which folders (under your home) the web may start them;
    --agents / --no-agents and --agent-folder NAME (repeatable, implies --agents) answer it
    ahead; without them and without a terminal (or with --yes) agents stay off."""
    try:
        sign_in(server, allow_runs=allow_runs, yes=yes, open_browser=browser, agents=agents, agent_folders=agent_folder)
    except LoginError as error:
        print(f"interact login: {error}", file=sys.stderr)
        raise SystemExit(1) from None


def logout() -> None:
    """Disconnect this computer: its machine and CLI key are revoked on the server, the background
    service stops, and its saved credentials are deleted."""
    try:
        sign_out()
    except LoginError as error:
        print(f"interact logout: {error}", file=sys.stderr)
        raise SystemExit(1) from None
