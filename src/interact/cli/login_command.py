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
    continue_conversations: bool | None = None,
    answer_approvals: bool | None = None,
) -> None:
    """Connect this computer to your Interact account: approve it in the browser, then it runs as
    one of your machines (kept connected by a background service: Linux systemd, Windows logon task), with your agents and prompts synced. --server defaults to
    the server you installed from or last signed in to, else it is asked once;
    --allow-runs lets this CLI start workflow runs (default: read only); --yes skips the final
    question; --no-browser only prints the page to open. Right after the approval it asks once
    whether agents may run here and in which folders (under your home) the web may start them;
    with agents on, whether the web may continue your editor conversations here (as a copy) and
    answer the approvals a session asks for. --agents / --no-agents, --agent-folder NAME
    (repeatable), --continue-conversations and --answer-approvals (each with --no-…; a folder or a
    yes implies --agents) answer them ahead; without them and without a terminal (or with --yes)
    agents stay off. On a computer already connected to this server it signs nothing in again and
    asks the same questions (or applies the same flags), the current settings as defaults."""
    try:
        sign_in(server, allow_runs=allow_runs, yes=yes, open_browser=browser, agents=agents, agent_folders=agent_folder,
                agent_opt_ins={"continue_conversations": continue_conversations, "answer_approvals": answer_approvals})
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
