"""Images drawn by the Galaius platform for this computer's person — the one way the `generate_image`
tool and `galaius images generate` get a picture: the company's provider connection draws it, each
image is a run charged to him (his ceilings and the company's apply) and listed in the month's
Facturation, linked to the agent run that asked. No provider key on this computer is used."""

import base64
from pathlib import Path
from uuid import UUID, uuid4

from galaius.caller import Caller
from galaius_core import ImageAnswer, ImageRequest
from pydantic import BaseModel, ConfigDict

from galaius.server_workspace import ServerWorkspace

#: Where drawn images go, under the folder the caller works in.
FOLDER = "galaius-images"
#: A file suffix per image type the platform answers with.
SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class DrawnImages(BaseModel):
    """What a draw left on disk: one file per image, what they cost together, what was not drawn."""

    model_config = ConfigDict(frozen=True)

    files: tuple[Path, ...]
    cost_usd: float | None
    failed: tuple[str, ...]
    runs: tuple[UUID, ...]

    def said(self) -> str:
        """The answer as an agent or a terminal reads it."""
        cost = "price unknown" if self.cost_usd is None else f"${self.cost_usd:.4f}"
        lines = [f"Saved {len(self.files)} image(s), {cost}, charged to you in Galaius (Facturation):", *(f"  {path}" for path in self.files)]
        lines += [f"  not drawn: {reason}" for reason in self.failed]
        return "\n".join(lines)


class Images:
    @staticmethod
    def inside(root: Path, folder: str) -> Path:
        """`folder` under `root`, never outside it (an agent names it; its run folder bounds it)."""
        target = (root / folder).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError(f"{folder} is outside the working folder {root}")
        return target

    @staticmethod
    def caller() -> UUID | None:
        """The agent run asking, when one does: the id galaius gave it at launch."""
        try:
            return UUID(Caller.environ()["GALAIUS_RUN_ID"])
        except (KeyError, ValueError):
            return None

    @classmethod
    def draw(cls, request: ImageRequest, folder: Path, idempotency_key: str | None = None) -> DrawnImages:
        """Draw `request` on the platform and save each image into `folder` (made when missing) as
        `image-<run>.png`; `idempotency_key`: the same key again answers the same images, paid once."""
        asked = request if request.caller_run is not None else request.model_copy(update={"caller_run": cls.caller()})
        answer: ImageAnswer = ServerWorkspace.configured().draw_images(asked, idempotency_key or str(uuid4()))
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for image in answer.images:
            path = folder / f"image-{str(image.run_id)[:8]}{SUFFIXES.get(image.media_type, '.bin')}"
            path.write_bytes(base64.b64decode(image.data_base64, validate=True))
            files.append(path.resolve())
        return DrawnImages(files=tuple(files), cost_usd=answer.cost_usd, failed=answer.failed, runs=tuple(image.run_id for image in answer.images))
