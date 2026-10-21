"""edit_video: a clip cut, joined, captioned on this PC by Kinocut, locally and at no cost (`galaius.video`)."""

import asyncio
import platform

from galaius_core import MACHINE_TOOLS, file_token
from pydantic import ValidationError

from galaius.machine_tools import ToolNotReady
from galaius.server.core import instrumented, mcp
from galaius.video import AnyStep, VideoEdit, edit


@mcp.tool(category="media")
@instrumented
async def edit_video(input: str, steps: list[AnyStep], output: str, replace: bool = False) -> str:
    """Edit a video in your working folder — cut it, join clips, caption it, lay text over it, resize it, take its
    sound — on this PC, at no cost, and show the result in the person's chat as a video they can play and download.

    Use this for every video edit meant for the person, never ffmpeg or a script of your own. `input` and every path are
    relative to your working folder; `steps` run in order, each on the result of the one before:

    - {"op": "trim", "start": 3}: keep from 3 s to the end (cut the first 3 seconds); "end": 10 stops at 10 s.
    - {"op": "join", "clips": ["b.mp4"], "transition": "fade"}: the video so far, then each clip (transition optional).
    - {"op": "caption", "text": "bonjour", "start": 0, "end": 2}: a subtitle burnt in at the bottom (end omitted: to the end).
    - {"op": "add_text", "text": "Title", "position": "center", "size": 48, "color": "white", "start": 0, "duration": 3}.
    - {"op": "resize", "width": 1280} or {"op": "resize", "aspect": "9:16"}.
    - {"op": "extract_audio"}: its sound alone, the last step, with an .mp3 or .wav `output`.

    `output`: where the result goes (.mp4 or .webm for a video; its suffix picks the format). replace: write over a file
    already there (refused otherwise). Returns where it was written, its length and size, and what it cost ($0), or why
    nothing was written.
    """
    try:
        spec = VideoEdit(input=input, steps=steps, output=output, replace=replace)
        edited = await asyncio.to_thread(edit, spec)
    except ValidationError as error:
        return "Nothing edited: " + "; ".join(str(problem["msg"]).removeprefix("Value error, ") for problem in error.errors())
    except ToolNotReady as error:
        return f"Nothing edited: {error}"
    except (ValueError, OSError) as error:
        return f"Nothing edited: {error}"
    written, tool = edited.written, MACHINE_TOOLS["video_editing"]
    facts = ", ".join(fact for fact in (f"{edited.seconds:.1f} s" if edited.seconds is not None else "", edited.resolution or "",
                                         f"{written.size:,} bytes") if fact)
    said = (f"Made by {tool.package} {tool.version} on this PC: video editing on {platform.node() or 'this PC'}: $0 (local, nothing charged).\n"
            f"  {written.path} ({facts})")
    # The token is how the chat finds the file to offer (the copy kept under that key), never a path.
    return said if written.key is None else f"{said} {file_token(written.key, written.path.name)}\nIt shows in the person's chat, to play and download."
