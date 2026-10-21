"""The Galaius platform's media jobs as agent tools (`galaius.media`): each a typed spec, a quote first, the job within
the cost the agent accepts, its files kept and shown in the person's chat. Never a provider key from this computer."""

import asyncio
import base64
import inspect
from collections.abc import Callable

from galaius_core import FILE_REFUSED, MediaImage, MediaSpec, Model3DSpec, MusicSpec, SoundSpec, SpeechSpec, TranslationSpec, image_type
from pydantic import ValidationError

from galaius.agents.catalog_connection import CatalogConnectionError
from galaius.media import FOLDER, Media
from galaius.server.core import instrumented, mcp

#: How every media tool costs money, and how its price is seen first: said in each one's description.
_PAID = """
It runs on the Galaius platform with your company's connection: charged to the person you work for, held to their
ceilings and the company's, and shown in Galaius › Facturation, linked to your run. Ask first WITHOUT max_cost_usd: the
answer is its price, nothing made, nothing charged. Then ask again with max_cost_usd (dollars, platform fee included)
at or above that price to make it. folder: where its files go, inside your working folder (made when missing).
"""


def _described(what: str, returns: str) -> str:
    return inspect.cleandoc(what) + "\n" + _PAID + "\n" + inspect.cleandoc(returns)


async def _asked(build: Callable[[], MediaSpec], *, max_cost_usd: float | None, folder: str) -> str:
    """The answer to one media tool call: `build()` makes its spec. A bad argument, a refusal: `FILE_REFUSED` and why, so
    the chat words the call as refused, never as a file made."""
    try:
        spec = build()
    except (ValidationError, ValueError, OSError) as error:
        return f"{FILE_REFUSED}{error}"
    try:
        made = await asyncio.to_thread(Media.ask, spec, max_cost_usd=max_cost_usd, folder=folder)
    except (CatalogConnectionError, ValueError, PermissionError, OSError) as error:
        return f"{FILE_REFUSED}{error}"
    return made.said()


@mcp.tool(category="media", description=_described(
    """Translate a text into another language with your company's text model, keeping its layout.

    Use this for every translation the person asks for. text: any length. target_language: a language tag
    (fr, en, en-GB, de, es…). source_language: when known (else read from the text). context: what the text is and for
    whom (« a reply to a client », « app labels »), so its register is kept.""",
    """Returns the price, or the translation and the file it is saved in (shown in the person's chat), or why nothing was
    made."""))
@instrumented
async def translate(text: str, target_language: str, source_language: str | None = None, context: str = "",
                    max_cost_usd: float | None = None, folder: str = FOLDER) -> str:
    return await _asked(lambda: TranslationSpec(text=text, target_language=target_language, source_language=source_language, context=context),
                        max_cost_usd=max_cost_usd, folder=folder)


@mcp.tool(category="media", description=_described(
    """Make a 3D model (a .glb file) of the object shown in an image, with your company's 3D model.

    image: the path of a PNG, JPEG or WebP image inside your working folder (one object on a plain background works best;
    make one with generate_image first when you only have a description). pbr: physically-based materials (slower).""",
    """Returns the price, or the .glb file made (shown in the person's chat), or why nothing was made."""))
@instrumented
async def generate_3d(image: str, pbr: bool = False, max_cost_usd: float | None = None, folder: str = FOLDER) -> str:
    def build() -> Model3DSpec:
        content = Media.read_input(image)
        if (kind := image_type(content)) is None:
            raise ValueError(f"{image} is no PNG, JPEG or WebP image")
        return Model3DSpec(image=MediaImage(media_type=kind, data_base64=base64.b64encode(content).decode()), pbr=pbr)
    return await _asked(build, max_cost_usd=max_cost_usd, folder=folder)


@mcp.tool(category="media", description=_described(
    """Say a text aloud with your company's voice model, as an audio file.

    Use this for every voice-over, narration or spoken message the person asks for. text: the words said. voice: the model's voice id; empty: its default. style: how to
    say it (« calm and warm », « cheerful »); empty: neutral.""",
    """Returns the price, or the audio file made (shown in the person's chat), or why nothing was made."""))
@instrumented
async def generate_speech(text: str, voice: str = "", style: str = "", max_cost_usd: float | None = None, folder: str = FOLDER) -> str:
    return await _asked(lambda: SpeechSpec(text=text, voice=voice, style=style), max_cost_usd=max_cost_usd, folder=folder)


@mcp.tool(category="media", description=_described(
    """Compose music from a description with your company's music model, as an audio file.

    Use this for every song, jingle or soundtrack the person asks for. prompt: genre, mood, instruments, tempo,
    structure, lyrics when it sings, and the length wanted.""",
    """Returns the price, or the music file made (shown in the person's chat), or why nothing was made."""))
@instrumented
async def generate_music(prompt: str, max_cost_usd: float | None = None, folder: str = FOLDER) -> str:
    return await _asked(lambda: MusicSpec(prompt=prompt), max_cost_usd=max_cost_usd, folder=folder)


@mcp.tool(category="media", description=_described(
    """Make a sound effect from a description with your company's sound model, as an audio file.

    Use this for every sound effect or ambience the person asks for (« rain on a tin roof », « a door creaking »), never
    for words said (generate_speech). seconds: its length, 0.5 to 30; empty: from the description. loop: made to repeat
    seamlessly.""",
    """Returns the price, or the sound file made (shown in the person's chat), or why nothing was made."""))
@instrumented
async def generate_sound(prompt: str, seconds: float | None = None, loop: bool = False, max_cost_usd: float | None = None, folder: str = FOLDER) -> str:
    return await _asked(lambda: SoundSpec(prompt=prompt, seconds=seconds, loop=loop), max_cost_usd=max_cost_usd, folder=folder)
