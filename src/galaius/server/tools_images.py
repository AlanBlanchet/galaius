"""generate_image: a picture drawn by the Galaius platform, never with a provider key from this computer."""

import asyncio
from pathlib import Path

from galaius_core import ImageBackground, ImageQuality, ImageRequest, ImageSize
from pydantic import ValidationError

from galaius.agents.catalog_connection import CatalogConnectionError
from galaius.images import FOLDER, Images
from galaius.server.core import instrumented, mcp


@mcp.tool(category="media")
@instrumented
async def generate_image(
    prompt: str,
    size: ImageSize | None = None,
    quality: ImageQuality | None = None,
    background: ImageBackground | None = None,
    n: int | None = None,
    model: str | None = None,
    folder: str = FOLDER,
) -> str:
    """Draw an image (up to 4) with your company's image model on the Galaius platform and save it here.

    Use this for every generated picture — never an image API called with a key from this computer:
    each image is charged to the person you work for, held to their ceilings and the company's, and
    shown in Galaius › Facturation, linked to your run.

    size: 1024x1024 (default), 1536x1024 (landscape), 1024x1536 (portrait) or auto. quality: low
    (cheapest), medium (default), high or auto. background: transparent for a cut-out (PNG with
    alpha), opaque or auto. n: images, 1 to 4, each its own charge. model: gpt-image-2 by default.
    folder: where the files go, inside your working folder (made when missing).

    Returns the saved file paths and what they cost, or why nothing was drawn (a ceiling reached,
    paid use of the provider off, the provider's refusal).
    """
    given = {"size": size, "quality": quality, "background": background, "n": n, "model": model}
    try:
        request = ImageRequest(prompt=prompt, **{name: value for name, value in given.items() if value is not None})
        target = Images.inside(Path.cwd(), folder)
    except (ValidationError, ValueError) as error:
        return f"No image drawn: {error}"
    try:
        drawn = await asyncio.to_thread(Images.draw, request, target)
    except CatalogConnectionError as error:
        return f"No image drawn: {error}"
    return drawn.said()
