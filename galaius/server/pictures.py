"""Pictures for the model that asked: what a tool captured goes back to the calling agent as images its own model
reads, sized to stay cheap. A caller whose model takes no image input keeps the text and is told why."""

import asyncio
import io
import logging
from collections.abc import Sequence
from typing import Self

from mcp.server.fastmcp.utilities.types import Image
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict

from galaius.account.caller import Caller
from galaius.agents import registry as run_registry
from galaius.catalog import models as model_catalog
from galaius.desktop.frames import Framing
from galaius.inference import ollama
from galaius.server.core import config

_log = logging.getLogger("galaius")

#: Tallest picture handed over, whatever its width: a phone capture at device scale 3 stays readable, a very long one
#: stays under what model APIs accept.
MAX_HEIGHT = 2048
#: Largest encoded picture handed over as PNG / GIF / WebP; above it, JPEG (model APIs refuse an image near 5 MB).
MAX_BYTES = 3_500_000
#: Most pictures one reply carries: an agent CLI refuses a tool result past its token budget.
MAX_PICTURES = 12
#: The encodings every vision model reads; anything else (BMP, TIFF) is re-encoded.
_READABLE = ("png", "jpeg", "gif", "webp")


class CallerModel(BaseModel):
    """The model calling this server, as far as it is known: its run's record names it. A client outside every run, or a
    model nothing describes, is taken to read images."""

    model_config = ConfigDict(frozen=True)
    id: str | None = None
    reads_images: bool = True

    @classmethod
    def current(cls) -> Self:
        try:
            run_id = Caller.current().environment.get("GALAIUS_RUN_ID")
            run = run_registry.get_run(run_id) if run_id else None
            if run is None or run.model is None:
                return cls()
            return cls(id=run.model, reads_images=cls._image_input(run.model) is not False)
        except (OSError, ValueError):  # a hint that cannot be read never costs the caller its capture
            _log.warning("the calling model could not be read; handing it the picture", exc_info=True)
            return cls()

    @staticmethod
    def _image_input(model: str) -> bool | None:
        """What the model's own source says: the local Ollama daemon for a model it serves (`name:tag`), else the
        cached provider catalog; None when neither knows."""
        if ":" in model or model.startswith("ollama/"):
            name = model.removeprefix("ollama/")
            local = next((found for found in ollama.discover_cached() if found.name == name and found.capabilities), None)
            if local is not None:
                return local.vision
        return model_catalog.reads_images(model)


class Picture(BaseModel):
    """One capture (`raw`, any image format) and the part of it the call asked for (`framing`)."""

    model_config = ConfigDict(frozen=True)
    raw: bytes
    framing: Framing = Framing()

    def shown(self, max_width: int) -> tuple[Image, str]:
        """The image to hand the caller, at most `max_width` wide (the call's own `max_width` wins) and `MAX_HEIGHT`
        tall; with a note mapping its points back to the capture when it is smaller than what the call framed."""
        capped = self.framing.model_copy(update={"max_width": self.framing.max_width or max_width, "max_height": MAX_HEIGHT})
        data, mapping = capped.apply(self.raw)
        note = "" if mapping == self.framing.apply(self.raw)[1] else f"smaller than the capture {mapping}"
        decoded = PILImage.open(io.BytesIO(data))
        encoding = (decoded.format or "").lower()
        source = (PILImage.open(io.BytesIO(self.raw)).format or "").lower()
        # A resize writes PNG: a JPEG source goes back as JPEG, a photograph being far larger as PNG.
        if encoding in _READABLE and len(data) <= MAX_BYTES and not (source == "jpeg" and encoding != "jpeg"):
            return Image(data=data, format=encoding), note
        if source != "jpeg":
            buffer = io.BytesIO()
            (decoded if decoded.mode != "CMYK" else decoded.convert("RGB")).save(buffer, format="PNG")
            if buffer.tell() <= MAX_BYTES:
                return Image(data=buffer.getvalue(), format="png"), note
        flat = PILImage.new("RGB", decoded.size, "white")
        layer = decoded.convert("RGBA")
        flat.paste(layer, mask=layer)
        buffer = io.BytesIO()
        flat.save(buffer, format="JPEG", quality=85)
        return Image(data=buffer.getvalue(), format="jpeg"), note


async def with_pictures(text: str, pictures: Sequence[Picture], *, wanted: bool = True, limit: int = MAX_PICTURES) -> tuple[str, list[Image]]:
    """`text`, with what the caller needs to know about its pictures, and the images to hand it (the first `limit`):
    none when it did not want them (`wanted`) or its model takes no image input (said in the text)."""
    if not (wanted and pictures):
        return text, []

    def prepare() -> tuple[str, list[Image]]:
        caller = CallerModel.current()
        if not caller.reads_images:
            return (f"{text}\n(no picture attached: your model {caller.id} takes no image input; pass query=… to have "
                    "galaius's vision model read the capture)"), []
        shown = [picture.shown(config.screenshot_max_width) for picture in pictures[:limit]]
        numbers: dict[str, list[str]] = {}  # each note once, with the pictures it is about
        for number, (_, note) in enumerate(shown, 1):
            if note:
                numbers.setdefault(note, []).append(str(number))
        notes = [f"(picture {', '.join(about)}: {note})" for note, about in numbers.items()]
        if len(pictures) > limit:
            notes.append(f"(pictures {limit + 1} to {len(pictures)} not attached: at most {limit} per reply)")
        return "\n".join([text, *notes]), [image for image, _ in shown]

    return await asyncio.to_thread(prepare)
