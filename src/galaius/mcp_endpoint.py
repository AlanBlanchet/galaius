"""Where this computer's shared galaius MCP server of a runtime listens, and how a relay starts it
(`galaius.mcp_relay.Endpoint`): what `galaius mcp` hands its relay, and what an older server names when
it tells a relay to move. Kept apart from the server so `galaius mcp` computes it without importing one."""

import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import NoReturn

from galaius import mcp_relay

from galaius.agents.ceiling import contained
from galaius.mcp_announcement import McpAnnouncement
from galaius.mcp_relay import Endpoint
from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES
from galaius.upgrade.store import Runtime, RuntimeStore


class SharedEndpoint:
    """Where a runtime's shared server listens and how a relay starts it (`galaius.mcp_relay.Endpoint`)."""

    @staticmethod
    def of(runtime: Runtime) -> Endpoint:
        """The endpoint of `runtime`'s server for this user and agents folder (a probe's own folder gets its own server)."""
        key = hashlib.sha256(f"{runtime.path}\0{UserPaths.agents()}".encode()).hexdigest()[:16]
        runtime_folder = os.environ.get("XDG_RUNTIME_DIR")
        folder = Path(runtime_folder) / "galaius" if runtime_folder else Path.home() / ".galaius" / "run"
        socket = folder / f"mcp-{key}.sock"
        start = contained([str(runtime.python), "-I", "-m", "galaius.server.shared", str(socket)])
        return Endpoint(socket=str(socket), start=start, log=str(RuntimeStore.default().root / "mcp-server.log"))

    @classmethod
    def for_client(cls, runtime: Runtime) -> Endpoint:
        """`runtime`'s endpoint with what to answer ahead while its server starts: its announcement with
        this client's gateway tools (`McpAnnouncement.with_gateway`, read in the client's own environment)."""
        endpoint = cls.of(runtime)
        site = runtime.site_packages
        announced = None if site is None else McpAnnouncement.installed(site)
        if announced is not None:
            ahead = Path(endpoint.socket).with_name(f"ahead-{os.getpid()}.json")
            PRIVATE_FILES.write_text(ahead, announced.with_gateway().model_dump_json())
            endpoint.ahead = str(ahead)
        return endpoint

    #: `0`: each client starts its own server under its upgrade supervisor, as on Windows.
    SWITCH_VARIABLE = "GALAIUS_SHARED_MCP"
    #: Set by a client that configures galaius through the server's environment (the VS Code extension: its keys,
    #: settings and model catalog): such a client keeps its own server, which a shared one could not honour.
    CONFIGURING = ("GALAIUS_MODELS_JSON", "GALAIUS_CONFIGURED_PROVIDERS")

    @classmethod
    def supported(cls) -> bool:
        """Clients share one server where Unix sockets and file locks exist (not Windows: there each
        client keeps its own server under its upgrade supervisor), unless `SWITCH_VARIABLE` says `0` or the
        client configures galaius through its environment (`CONFIGURING`)."""
        return (sys.platform != "win32" and hasattr(socket, "AF_UNIX") and os.environ.get(cls.SWITCH_VARIABLE, "1") != "0"
                and not any(os.environ.get(name) for name in cls.CONFIGURING))

    @classmethod
    def relay(cls, runtime: Runtime) -> NoReturn:
        """Become this client's relay to `runtime`'s shared server: the standard library alone, run as a
        file (`-I -S`: no package of this process stays loaded)."""
        python = str(runtime.python)
        os.execv(python, [python, "-I", "-S", mcp_relay.__file__, json.dumps(cls.for_client(runtime).dumped())])
