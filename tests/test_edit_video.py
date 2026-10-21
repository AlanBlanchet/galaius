"""edit_video: the capability installed on this PC (its pinned Kinocut in a venv of its own) cuts, joins and captions a
real clip, and the result is kept for the asking run's chat; before its install it does nothing and says where."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from galaius_core import MACHINE_TOOLS, file_key

from galaius.agents import registry as reg
from galaius.fence import FenceSpec
from galaius.machine_tools import MachineTools, ToolNotReady
from galaius.video import VideoEdit, edit
from tests.support import register_run

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("uv")), reason="needs ffmpeg, ffprobe and uv")


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real install for the module: `uv` puts the pinned Kinocut in the capability's own venv."""
    root = tmp_path_factory.mktemp("tools")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MachineTools, "root", root)
        assert MachineTools.state("video_editing").phase == "absent"
        MachineTools._job("video_editing", __import__("threading").Event())
        assert MachineTools.state("video_editing").ready, MachineTools.state("video_editing")
    return root


@pytest.fixture(autouse=True)
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: Path) -> None:
    monkeypatch.setattr(MachineTools, "root", tools)
    monkeypatch.chdir(tmp_path)
    register_run(run_id="editor")
    monkeypatch.setenv("GALAIUS_RUN_ID", "editor")
    for name, source in (("clip.mp4", "testsrc"), ("b.mp4", "testsrc2")):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"{source}=duration=3:size=320x240:rate=25", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=3", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", name], check=True)


def _seconds(path: Path) -> float:
    probed = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return float(json.loads(probed.stdout)["format"]["duration"])


@pytest.mark.parametrize(("steps", "output", "seconds"), [
    ([{"op": "trim", "start": 1}], "out/cut.mp4", 2.0),                                   # « cut the first second »
    ([{"op": "trim", "start": 1}, {"op": "caption", "text": "bonjour"}], "out/cut.webm", 2.0),
    ([{"op": "join", "clips": ["b.mp4"]}, {"op": "caption", "text": "bonjour", "end": 1}], "joined.mp4", 6.0),
])
def test_a_clip_is_cut_joined_and_captioned_here_and_kept_for_the_runs_chat(tmp_path: Path, steps: list, output: str, seconds: float) -> None:
    edited = edit(VideoEdit(input="clip.mp4", steps=steps, output=output))
    written = tmp_path / output
    assert edited.written.path == written and abs(_seconds(written) - seconds) < 0.2 and edited.written.size == written.stat().st_size
    assert edited.written.key == file_key(written.read_bytes(), written.suffix)
    assert reg.RunFiles.path("editor", edited.written.key).read_bytes() == written.read_bytes()


def test_a_fenced_run_edits_only_a_clip_it_may_read_and_nothing_starts_before_the_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kinocut reads copies galaius made from galaius's own process: the run's levels decide what it may hand over. Before
    its owner installs the capability, the tool does nothing and names where to install it."""
    (tmp_path / "work").mkdir()
    (tmp_path / "private").mkdir()
    shutil.move("clip.mp4", tmp_path / "private" / "clip.mp4")
    register_run(run_id="fenced", fence=FenceSpec(working_directory=tmp_path, levels={"work": "write", "private": "hidden"}, start=tmp_path / "work", state=tmp_path / "state"))
    monkeypatch.setenv("GALAIUS_RUN_ID", "fenced")
    with pytest.raises(PermissionError, match="level « hidden »"):
        edit(VideoEdit(input="private/clip.mp4", steps=[{"op": "trim", "start": 1}], output="work/cut.mp4"))
    monkeypatch.setattr(MachineTools, "root", tmp_path / "no-tools")
    with pytest.raises(ToolNotReady, match=f"{MACHINE_TOOLS['video_editing'].label} is not installed on this PC.*Install"):
        edit(VideoEdit(input="private/clip.mp4", steps=[{"op": "trim", "start": 1}], output="work/cut.mp4"))
    assert not (tmp_path / "work" / "cut.mp4").exists()
