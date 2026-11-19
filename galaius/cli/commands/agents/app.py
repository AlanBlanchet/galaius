"""The `agents` command group and the refusal line its commands share."""

from typing import TextIO
from cyclopts import App
from galaius_core import MachineRefusal, RefusedLine


agents_app = App(name="agents", help="Spawn and supervise agent runs across providers.")


def said_refused(sentence: str, refusal: MachineRefusal | None, *, file: TextIO | None = None) -> None:
    """A refusal said to whoever runs the command: its `ERROR:` line, then, typed, its reason as one `RefusedLine`."""
    print(f"ERROR: {sentence}" if not sentence.startswith("ERROR: ") else sentence, file=file)
    if refusal is not None:
        print(RefusedLine(refusal=refusal).model_dump_json(), file=file)
