"""MCP servers older than the installed release, and killing them on request."""

from galaius.mcp.registry import kill_stale_servers, latest_version, stale_servers


def print_stale_servers(indent: str = "  ", fix: bool = False) -> None:
    """Flag any RUNNING MCP server that loaded an OLDER version than is now available — the silent
    stale-server trap (a long-lived `galaius mcp` keeps serving code imported at startup, so a
    shipped fix never reaches it until reconnected). Names the pid so the user knows which editor
    window's server to reconnect. With ``fix=True`` (``galaius doctor --fix``) it restarts them
    itself (SIGTERM → editor respawns galaius on current code). Nothing printed when every server
    is current."""

    stale = stale_servers()
    if not stale:
        return
    latest = latest_version()
    if fix:
        killed = kill_stale_servers()  # only pids CONFIRMED terminated — never an unverified claim
        unresolved = [s for s in stale if s.get("pid") not in killed]
        if killed:
            pids = ", ".join(str(p) for p in killed)
            print(f"{indent}✓ stopped {len(killed)} stale MCP server(s) (pid {pids}), each was serving "
                  f"code older than v{latest}. Most hosts start a fresh v{latest} process on their next "
                  f"tool call — if galaius tools are still missing afterward, reconnect (or fully "
                  f"restart) that MCP connection in your editor; a stopped process is not a guarantee "
                  f"every host reconnects on its own (#144).")
        if unresolved:
            pids = ", ".join(str(s.get("pid")) for s in unresolved)
            print(f"{indent}⚠ could not confirm {len(unresolved)} stale server(s) stopped (pid {pids}) — "
                  f"not galaius mcp, already gone, or no permission to signal it. Reconnect that MCP "
                  f"connection (or fully restart the editor) to load v{latest}.")
        return
    print(f"{indent}⚠ stale MCP server(s) — serving code older than this tree; reconnect to load fixes")
    print(f"{indent}   (add --fix to this command to stop them):")
    for s in stale:
        if s.get("reason") == "code":
            # The common case between releases: the version never moved, the code did.
            print(f"{indent}    pid {s['pid']}: v{s.get('version')} — started before the last edit")
        else:
            print(f"{indent}    pid {s['pid']}: v{s.get('version')} (tree is v{latest})")
