"""Syncing the agent catalog, its policy hook and community agents."""

import json
import sys
from pathlib import Path
from typing import Annotated
from cyclopts import Parameter
from pydantic import ValidationError
from galaius_core import CommunityKind
from galaius.agents.catalog.community import CommunityAgent
from galaius.agents.catalog.entries import AgentCatalog
from galaius.agents.catalog.connection import CatalogConnection, CatalogConnectionError
from galaius.cli.commands.agents.app import agents_app


@agents_app.command(name="sync")
def agents_sync(
    endpoint: Annotated[str | None, Parameter(name="--endpoint")] = None,
    machine: bool = False,
    workspace: Annotated[str | None, Parameter(name="--workspace")] = None,
    token_file: Annotated[Path | None, Parameter(name="--token-file")] = None,
) -> None:
    """Refresh the server-owned role catalog; save connection settings after validation.

    First sync requires --machine (this PC's own link: its server, company and machine
    token) or --endpoint with --token-file. Later syncs reuse the saved connection.
    --workspace selects a workspace UUID; otherwise the authenticated bootstrap supplies it.
    """
    try:
        if machine:
            if endpoint is not None or workspace is not None or token_file is not None:
                raise ValueError("--machine takes the server and company from this PC's link")
            connection = CatalogConnection.linked()
            if connection is None:
                raise ValueError("this PC is not linked to a Galaius server; run `galaius login`")
        elif endpoint is None:
            if workspace is not None or token_file is not None:
                raise ValueError("connection changes require an explicit --endpoint")
            connection = CatalogConnection.load()
            if connection is None:
                raise ValueError("first sync requires --machine, or --endpoint with --token-file")
        else:
            if token_file is None:
                raise ValueError("--endpoint requires --token-file")
            connection = CatalogConnection(
                endpoint=endpoint.rstrip("/"), workspace_id=workspace,
                auth_mode="token", token_file=token_file.absolute(),
            )
        catalog = AgentCatalog.refresh(connection)
        catalog.connection.save()
    except ValidationError:
        print("ERROR: invalid catalog connection; check endpoint, workspace UUID and auth mode", file=sys.stderr)
        raise SystemExit(2) from None
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({
        "status": "current", "workspace": str(catalog.connection.workspace_id),
        "agents": len(catalog.snapshot.agents), "paradigms": len(catalog.snapshot.paradigms),
        "cursor": catalog.snapshot.cursor, "fetched_at": catalog.fetched_at.isoformat(),
    }))


@agents_app.command(name="policy-hook")
def agents_policy_hook() -> None:
    """Apply the shared agent policy to Codex lifecycle events on stdin; no model call."""
    from galaius.hooks.codex_policy import main

    raise SystemExit(main())


@agents_app.command(name="community")
def agents_community(need: str, kind: CommunityKind | None = None) -> None:
    """Search the community library for an agent, skill or prompt made for NEED; start one with
    `galaius agents spawn --community <id> "<task>"`."""
    try:
        print(CommunityAgent.search(need, kind))
    except CatalogConnectionError as error:
        print(f"ERROR: the community library could not be read: {error}", file=sys.stderr)
        raise SystemExit(2) from None
