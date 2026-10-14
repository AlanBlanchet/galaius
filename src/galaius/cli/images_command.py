"""`galaius images generate "a red fox, flat illustration" --size 1536x1024 --out pictures/` — an image
drawn by the Galaius platform with the company's image model, charged to you and shown in Facturation;
never a provider key from this computer. Prints what was saved as JSON; exit 0 at least one image
saved, 2 nothing drawn (the refusal on stderr)."""

import json
import sys
from pathlib import Path

from cyclopts import App

from galaius_core import ImageBackground, ImageQuality, ImageRequest, ImageSize

from galaius.agents.catalog_connection import CatalogConnectionError
from galaius.images import FOLDER, Images

images_app = App(name="images", help="Draw images on the Galaius platform: charged to you, never with a local key.")


@images_app.command(name="generate")
def images_generate(prompt: str, *, size: ImageSize | None = None, quality: ImageQuality | None = None, background: ImageBackground | None = None, n: int | None = None,
                    model: str | None = None, out: Path = Path(FOLDER), idempotency_key: str | None = None) -> None:
    """Draw PROMPT and save each image into --out.

    Parameters
    ----------
    prompt
        What to draw: subject, style, light, framing.
    size
        1024x1024 (default), 1536x1024 (landscape), 1024x1536 (portrait) or auto.
    quality
        low (cheapest), medium (default), high or auto.
    background
        transparent for a cut-out (PNG with alpha), opaque or auto.
    n
        Images to draw, 1 to 4; each is its own charge.
    model
        The image model, as the company's connection offers it (gpt-image-2 by default).
    out
        Folder to save into (made when missing).
    idempotency_key
        Reuse in a retried script: the same key answers the images it already drew, paid once.
    """
    try:
        given = {"size": size, "quality": quality, "background": background, "n": n, "model": model}
        drawn = Images.draw(ImageRequest(prompt=prompt, **{name: value for name, value in given.items() if value is not None}), out, idempotency_key)
    except (CatalogConnectionError, ValueError) as error:
        print(f"no image drawn: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"files": [str(path) for path in drawn.files], "cost_usd": drawn.cost_usd, "failed": list(drawn.failed), "runs": [str(run) for run in drawn.runs]}))
