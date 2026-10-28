"""record(scene=…): an animated page a community agent wrote, played alone and saved as a video for the person's chat."""

import asyncio
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from galaius.agents.community import CommunityAgent
from galaius.machine_tools import MachineTools
from galaius.video import Scene
from tests.support import register_run


def test_a_stranger_s_scene_becomes_a_video_and_reaches_nothing_of_this_pc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Motion design through a community agent (2026-10-27): its page is recorded, while the beacon it holds, its script
    and its frame onto a file of this PC go nowhere (threat model 2026-10-27: a file:// frame showed config.env, an img
    reached the network)."""
    hits: list[str] = []

    class Beacon(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Beacon)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    beacon = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(MachineTools, "root", tmp_path / "tools")
    monkeypatch.chdir(tmp_path)
    work = tmp_path / ".galaius-community" / "child"
    work.mkdir(parents=True)
    (tmp_path / "config.env").write_text("TOKEN=secret")
    (tmp_path / "secret.html").write_text("<body style='margin:0;background:#00ff00;height:2000px'></body>")
    (work / "clip.html").write_text(
        f'<style>@keyframes m {{ to {{ transform: translateX(600px) }} }} div {{ width: 80px; height: 80px; background: #e33; animation: m 1s infinite }}</style>'
        f'<div></div><img src="{beacon}/img"><link rel="stylesheet" href="{beacon}/css"><iframe src="file://{tmp_path}/config.env"></iframe><iframe src="file://{tmp_path}/secret.html" width="300" height="150"></iframe>'
        f'<script>fetch("{beacon}/script")</script><svg><image href="{beacon}/svg"/></svg>')
    agent = CommunityAgent(entry=uuid4(), kind="skill", title="Motion", author="someone/skills", instructions="Animate.", digest="0" * 64, nonce="n0nce", work=work)
    register_run(run_id="coordinator", cwd=str(tmp_path))
    register_run(run_id="child", parent_run_id="coordinator", community=agent)
    monkeypatch.setenv("GALAIUS_RUN_ID", "coordinator")
    try:
        written = asyncio.run(Scene(scene=str(work / "clip.html"), output="clip.webm", seconds=1.5, width=320, height=180).record())
    except ValueError as error:
        pytest.skip(str(error))
    finally:
        server.shutdown()
    assert written.path == tmp_path / "clip.webm" and written.size > 1000 and written.path.read_bytes()[:4] == b"\x1a\x45\xdf\xa3"
    assert hits == []
    if shutil.which("ffmpeg"):
        frame = tmp_path / "frame.png"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(written.path), "-frames:v", "1", str(frame)], check=True)
        pixels = Image.open(frame).convert("RGB").getdata()
        assert not any(green > 200 and red < 60 and blue < 60 for red, green, blue in pixels), "a file of this PC was drawn in the scene"
    with pytest.raises(ValueError, match="at most"):
        Scene(scene="clip.html", output="big.webm", seconds=1, width=1280, height=1280)
    with pytest.raises(PermissionError, match="only a file in the folder"):
        asyncio.run(Scene(scene=str(tmp_path / "config.env"), output="leak.webm", seconds=1).record())
