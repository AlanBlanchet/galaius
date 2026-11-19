"""A PC pulls the files a message names from the server's relay by key, checks them against the key, and keeps them for the run as the person's."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from galaius_core import OWNER_MAKER, AttachedFile, file_key

from galaius.agents import registry as reg
from galaius.machine.agents import MachineAgents
from galaius.machine.projects.workspace_copy import WorkspaceRefused, WorkspaceTransfers
from tests.support import register_run

HELD = {file_key(b"recording bytes " * 1000, ".mkv"): b"recording bytes " * 1000, file_key(b"expected", ".txt"): b"tampered!"}


class Relay(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        key = self.path.rsplit("/", 1)[-1]
        body = HELD.get(key)
        self.send_response(404 if body is None else 200)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def transfers():
    server = HTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield WorkspaceTransfers(endpoint=lambda path: f"http://127.0.0.1:{server.server_port}{path}", headers={"authorization": "Bearer t"}, timeout=5)
    server.shutdown()


def test_a_file_pulled_by_key_is_checked_against_it_and_kept_for_the_run_as_the_persons(transfers, tmp_path: Path) -> None:
    key = next(iter(HELD))
    target = tmp_path / "pulled"
    assert transfers.pull_file(key, target) == len(HELD[key]) and target.read_bytes() == HELD[key]
    for refused, why in ((file_key(b"nobody", ".txt"), "does not hold"), (file_key(b"expected", ".txt"), "damaged")):
        with pytest.raises(WorkspaceRefused, match=why):
            transfers.pull_file(refused, tmp_path / "x")
    register_run(run_id="agent")
    kept = MachineAgents._keep_attached("agent", ((AttachedFile(key=key, name="take 1.mkv"), target),))
    (handed,) = kept
    assert (handed.name, handed.story.maker) == ("take 1.mkv", OWNER_MAKER) and reg.RunFiles.path("agent", key).read_bytes() == HELD[key]
    assert reg.RunFiles.entry("agent", key).story.maker == OWNER_MAKER
