"""Desktop sandboxes left behind by dead runs."""

import subprocess
from galaius.desktop import orphans


def print_sandboxes(real_display: str | None) -> None:
    """Which sandbox displays exist right now, and who owns each.

    Answers "why do I see two Xephyr windows?" — every galaius server owns its own sandbox, so
    one window per server that launched something is BY DESIGN. Not by design: a window whose
    owner is gone, called out here and swept on the next launch.
    """

    try:
        servers = orphans._list_x_servers()
    except (OSError, subprocess.SubprocessError):
        return
    if not servers:
        return
    print(f"  sandboxes     : {len(servers)} open (one per galaius server that has launched an app)")
    for srv in servers:
        where = orphans.display_of(srv.cmdline) or "?"
        clients = len(orphans.display_clients(where)) if where != real_display else 0
        owner = "ORPHANED — no owner left; swept on the next launch" if orphans.is_orphan(srv) \
            else f"owner pid {srv.ppid}"
        print(f"                  {where}  {clients} app(s)  ({owner})")
