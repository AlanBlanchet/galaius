"""Video editing on this PC (`edit_video`): a typed list of steps — cut, join, caption, overlay text, resize, extract the
sound — run by Kinocut over FFmpeg, locally and at no cost, once its owner installed the capability here
(`MachineTools`, `video_editing`).

Every input is copied, without a link and only from where the asking run may read, into a private folder of its own;
Kinocut sees only those copies, offline (every proxy points nowhere), one step after the other. The result is saved
like a file `create_file` writes (`files.save`: only where the run may write, kept for the chat in parts, never whole)."""

import json
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Annotated, ClassVar, Literal, NamedTuple

from galaius_core import KEPT_FILE_MAX, MACHINE_TOOLS
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from galaius.files import Written, copy_working, kept_path, offline_environment, save
from galaius.machine_tools import MachineTools

#: What this capability writes (`MACHINE_TOOLS`), split into video and sound by content type.
_WRITES = dict(row for tool in MACHINE_TOOLS["video_editing"].tools for row in tool.writes)
VIDEO_OUT = tuple(suffix for suffix, kind in _WRITES.items() if kind.startswith("video/"))
AUDIO_OUT = tuple(suffix for suffix, kind in _WRITES.items() if kind.startswith("audio/"))
#: The videos a step reads: what this capability writes, and what a camera or a phone records.
VIDEO_IN = (*VIDEO_OUT, ".mov", ".mkv", ".m4v", ".avi")

InputPath = Annotated[str, AfterValidator(lambda path: kept_path(path, VIDEO_IN))]
OutputPath = Annotated[str, AfterValidator(lambda path: kept_path(path, (*VIDEO_OUT, *AUDIO_OUT)))]
Seconds = Annotated[float, Field(ge=0, le=24 * 3600)]
Text = Annotated[str, Field(min_length=1, max_length=500)]


class Step(BaseModel, ABC):
    """One edit Kinocut makes on the video so far: its command, its options, then (after `--`) its inputs."""

    model_config = ConfigDict(extra="forbid")
    command: ClassVar[str]

    @abstractmethod
    def options(self, work: Path) -> list[str]:
        """Its command's options (`work`: the private folder, for a file the step writes first)."""

    def inputs(self, video: Path, clips: dict[str, Path]) -> list[str]:
        return [str(video)]

    def reads(self) -> tuple[str, ...]:
        """The input paths it names besides the video so far."""
        return ()


class Trim(Step):
    """Keep `start` to `end` seconds (`end` None: to its end): « cut the first 3 seconds » is `start` 3."""

    op: Literal["trim"]
    start: Seconds = 0
    end: Seconds | None = None
    command: ClassVar[str] = "trim"

    @model_validator(mode="after")
    def kept(self) -> "Trim":
        if self.end is not None and self.end <= self.start:
            raise ValueError("a trim keeps from start to an end after it")
        return self

    def options(self, work: Path) -> list[str]:
        return ["--accurate", "-s", f"{self.start:g}", *(["-e", f"{self.end:g}"] if self.end is not None else [])]


class Join(Step):
    """The video so far, then each of `clips` after it, in order (`transition` between each two, None: a straight cut)."""

    op: Literal["join"]
    clips: list[InputPath] = Field(min_length=1, max_length=20)
    transition: Literal["fade", "dissolve", "wipe-left", "wipe-right", "wipe-up", "wipe-down"] | None = None
    command: ClassVar[str] = "merge"

    def options(self, work: Path) -> list[str]:
        return ["-t", self.transition] if self.transition else []

    def inputs(self, video: Path, clips: dict[str, Path]) -> list[str]:
        return [str(video), *(str(clips[clip]) for clip in self.clips)]

    def reads(self) -> tuple[str, ...]:
        return tuple(self.clips)


class Caption(Step):
    """`text` burnt in as a subtitle from `start` to `end` seconds (None: to the end), at the bottom."""

    op: Literal["caption"]
    text: Text
    start: Seconds = 0
    end: Seconds | None = None
    command: ClassVar[str] = "subtitles"

    def options(self, work: Path) -> list[str]:
        return []

    def inputs(self, video: Path, clips: dict[str, Path]) -> list[str]:
        subtitle = video.with_name(f"{video.stem}.srt")
        lines = " ".join(self.text.split("\n\n")).strip()
        subtitle.write_text(f"1\n{_srt(self.start)} --> {_srt(self.end if self.end is not None else 24 * 3600 - 1)}\n{lines}\n", encoding="utf-8")
        return [str(video), str(subtitle)]


class AddText(Step):
    """`text` laid over the picture at `position`, `size` pixels, in `color`, from `start` for `duration` seconds
    (None: to the end)."""

    op: Literal["add_text"]
    text: Text
    position: Literal["top-left", "top-center", "top-right", "center-left", "center", "center-right",
                      "bottom-left", "bottom-center", "bottom-right"] = "center"
    size: int = Field(default=48, ge=8, le=400)
    color: str = Field(default="white", pattern=r"^(?:[a-z]{3,20}|#[0-9a-fA-F]{6})$")
    start: Seconds = 0
    duration: Seconds | None = None
    command: ClassVar[str] = "add-text"

    def options(self, work: Path) -> list[str]:
        return ["-p", self.position, "--size", str(self.size), "--color", self.color, "--start-time", f"{self.start:g}",
                *(["--duration", f"{self.duration:g}"] if self.duration is not None else [])]

    def inputs(self, video: Path, clips: dict[str, Path]) -> list[str]:
        return [str(video), self.text]


class Resize(Step):
    """To `width` × `height` pixels, or to an `aspect` ratio (a phone's 9:16)."""

    op: Literal["resize"]
    width: int | None = Field(default=None, ge=16, le=7680)
    height: int | None = Field(default=None, ge=16, le=4320)
    aspect: Literal["16:9", "9:16", "1:1", "4:3", "4:5", "21:9"] | None = None
    command: ClassVar[str] = "resize"

    @model_validator(mode="after")
    def sized(self) -> "Resize":
        if self.width is None and self.height is None and self.aspect is None:
            raise ValueError("a resize names a width, a height or an aspect")
        return self

    def options(self, work: Path) -> list[str]:
        return [*(["-w", str(self.width)] if self.width else []), *(["--height", str(self.height)] if self.height else []),
                *(["-a", self.aspect] if self.aspect else [])]


class ExtractAudio(Step):
    """Its sound alone, in the format `output` names (.mp3, .wav): the last step."""

    op: Literal["extract_audio"]
    command: ClassVar[str] = "extract-audio"

    def options(self, work: Path) -> list[str]:
        return []


class Convert(Step):
    """Into another video format: the step `VideoEdit` adds when `output` names one the video so far is not in."""

    op: Literal["convert"] = "convert"
    to: str
    command: ClassVar[str] = "convert"

    def options(self, work: Path) -> list[str]:
        return ["-f", self.to]


AnyStep = Annotated[Trim | Join | Caption | AddText | Resize | ExtractAudio, Field(discriminator="op")]


class VideoEdit(BaseModel):
    """`input` (in the working folder), edited by `steps` in order, saved at `output` (its suffix picks the format)."""

    model_config = ConfigDict(extra="forbid")

    input: InputPath
    steps: list[AnyStep] = Field(min_length=1, max_length=20)
    output: OutputPath
    replace: bool = False

    @model_validator(mode="after")
    def sound_last(self) -> "VideoEdit":
        audio = PurePosixPath(self.output).suffix.lower() in AUDIO_OUT
        if any(isinstance(step, ExtractAudio) for step in self.steps[:-1]) or audio != isinstance(self.steps[-1], ExtractAudio):
            raise ValueError(f"extract_audio is the last step, and only then is the output a sound ({', '.join(AUDIO_OUT)})")
        return self

    @property
    def reads(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.input, *(path for step in self.steps for path in step.reads()))))


class Edited(NamedTuple):
    written: Written
    #: What Kinocut says of the result: its length in seconds and its picture's size (None for a sound).
    seconds: float | None
    resolution: str | None


class Kinocut:
    """Kinocut's command line, run on the private copies only, offline, one step at a time."""

    #: Longest one step may take (a long clip re-encoded on a CPU).
    STEP_SECONDS: ClassVar[float] = 1800

    def __init__(self, program: Path) -> None:
        self.program = program
        self.environment = {**offline_environment(), "KINOCUT_FFMPEG_EXECUTABLE": shutil.which("ffmpeg") or "ffmpeg"}

    def run(self, step: Step, video: Path, clips: dict[str, Path], output: Path, work: Path) -> dict:
        argv = [str(self.program), "--format", "json", step.command, *step.options(work), "-o", str(output), "--", *step.inputs(video, clips)]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=self.STEP_SECONDS, cwd=work, env=self.environment, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ValueError(f"{step.command} did not finish within {self.STEP_SECONDS / 60:.0f} min") from None
        try:
            said = json.loads(done.stdout)
        except ValueError:
            said = {}
        if not isinstance(said, dict) or not said.get("success") or not output.is_file():
            error = said.get("error") if isinstance(said, dict) else None
            message = str(error.get("message") if isinstance(error, dict) else "") or (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]
            raise ValueError(f"{step.command} failed: {message.replace(str(work) + os.sep, '')[:300]}")
        return said


def edit(spec: VideoEdit) -> Edited:
    """`spec` run here: its inputs read only where the asking run may read, the result saved where it may write."""
    kinocut = Kinocut(MachineTools.program("video_editing"))
    work_root = MachineTools.folder("video_editing") / "work"
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="edit-", dir=work_root) as folder:
        work = Path(folder)
        copies = {path: work / f"in-{index}{PurePosixPath(path).suffix.lower()}" for index, path in enumerate(spec.reads)}
        for path, copy in copies.items():
            copy_working(path, copy, KEPT_FILE_MAX)  # read only where the asking run may read, never through a link
        video, said = copies[spec.input], {}
        target = PurePosixPath(spec.output).suffix.lower()
        for index, step in enumerate(spec.steps):
            output = work / f"step-{index}{target if isinstance(step, ExtractAudio) else video.suffix if video.suffix in VIDEO_OUT else '.mp4'}"
            said, video = kinocut.run(step, video, copies, output, work), output
        if video.suffix != target:
            output = work / f"converted{target}"
            said, video = kinocut.run(Convert(to=target[1:]), video, copies, output, work), output
        written = save(spec.output, video, replace=spec.replace)
    duration, resolution = said.get("duration"), said.get("resolution")
    return Edited(written, float(duration) if isinstance(duration, int | float) else None, str(resolution) if resolution and target in VIDEO_OUT else None)


def _srt(seconds: float) -> str:
    """`seconds` as a SubRip time: 00:00:03,250."""
    millis = round(seconds * 1000)
    return f"{millis // 3_600_000:02d}:{millis // 60_000 % 60:02d}:{millis // 1000 % 60:02d},{millis % 1000:03d}"
