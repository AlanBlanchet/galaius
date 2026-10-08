"""`galaius login` / `galaius logout`: this computer joins or leaves a Galaius account."""

import sys
from typing import Annotated

from cyclopts import Parameter

from galaius.account_login import LoginError, login as sign_in, logout as sign_out


def login(
    server: Annotated[str | None, Parameter(name="--server")] = None,
    *,
    allow_runs: bool = False,
    browser: bool = True,
    detach: bool = False,
    resume: Annotated[bool, Parameter(show=False)] = False,
    agents: bool | None = None,
    agent_folder: Annotated[tuple[str, ...], Parameter(name="--agent-folder")] = (),
    continue_conversations: bool | None = None,
    answer_approvals: bool | None = None,
) -> None:
    """Connect this computer to your Galaius account, asking nothing: approve it in the browser
    (opened on its page), then it runs as one of your machines, kept connected by a background
    service (Linux systemd, macOS launchd, Windows logon task), online within a minute or told why,
    here and on its page. Every choice (agents, their folders, folder levels, its name) is made on
    that page; until then agents stay off and every folder is hidden. --server defaults to the
    server you installed from or last signed in to; --allow-runs lets this CLI start workflow runs
    (default: read only); --no-browser only prints the page to open; --detach (the install line)
    hands the waiting to a background process and returns at once with one line. --agents /
    --no-agents, --agent-folder NAME (repeatable), --continue-conversations and --answer-approvals
    (each with --no-…; a folder or a yes implies --agents) set them ahead for scripts. On a computer
    already connected to this server it signs nothing in again: the flags apply and its service
    restarts on this build."""
    try:
        sign_in(server, allow_runs=allow_runs, open_browser=browser, detach=detach, resume=resume, agents=agents, agent_folders=agent_folder,
                agent_opt_ins={"continue_conversations": continue_conversations, "answer_approvals": answer_approvals})
    except LoginError as error:
        print(f"galaius login: {error}", file=sys.stderr)
        raise SystemExit(1) from None


def logout() -> None:
    """Disconnect this computer: its machine and CLI key are revoked on the server, the background
    service stops, and its saved credentials are deleted."""
    try:
        sign_out()
    except LoginError as error:
        print(f"galaius logout: {error}", file=sys.stderr)
        raise SystemExit(1) from None
