"""Every media tool asks the platform the same way (`galaius.media`): its price first, then the job within the cost the
agent accepts; what comes back is saved in the working folder and kept for the asking run like a created file, so the
person's chat shows it as a file to download. Never a provider key from this computer."""

import asyncio
import base64
from pathlib import Path
from uuid import uuid4

import pytest
from galaius_core import FILE_REFUSED, MediaAnswer, MediaFile, MediaQuote, created_files

from galaius.agents import registry as reg
from galaius.server import tools_media
from galaius.server_workspace import PlatformRefused, ServerWorkspace
from tests.support import register_run

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")
MESH = b"glTF\x02\x00\x00\x00" + b"\x00" * 32


class Platform:
    """Stands in for the server's `POST /media` at the one client call: a quote without a cap, the job within it."""

    def __init__(self, made: tuple[str, bytes, str | None], refusal: str | None = None) -> None:
        self.made, self.refusal, self.asked, self.keys = made, refusal, [], []
        self.run = uuid4()

    def make_media(self, request, idempotency_key):
        self.asked.append(request)
        self.keys.append(idempotency_key)
        if self.refusal:
            raise PlatformRefused(self.refusal)
        quote = MediaQuote(provider="fal", model="tripo3d/h3.1/image-to-3d", low_usd=0.315, high_usd=0.315)
        if request.max_cost_usd is None:
            return MediaAnswer(kind=request.spec.kind, quote=quote)
        name, content, text = self.made
        return MediaAnswer(kind=request.spec.kind, quote=quote, run_id=self.run, cost_usd=0.315, text=text,
                           files=(MediaFile(name=name, media_type="model/gltf-binary", data_base64=base64.b64encode(content).decode(), size_bytes=len(content)),))


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    register_run(run_id="maker")
    monkeypatch.setenv("GALAIUS_RUN_ID", "maker")
    return tmp_path


JOBS = {
    # A file name the platform gives is never a path: only its last part is used, inside the folder asked.
    "3d model": (lambda cap: tools_media.generate_3d("photo.png", max_cost_usd=cap), ("../../model-ab12.glb", MESH, None), "galaius-media/model-ab12.glb"),
    "translation": (lambda cap: tools_media.translate("Bonjour", "en", max_cost_usd=cap), ("translation-ab12.en.txt", b"Hello", "Hello"), "galaius-media/translation-ab12.en.txt"),
    "speech": (lambda cap: tools_media.generate_speech("Bonjour à tous", style="warm", max_cost_usd=cap), ("speech-ab12.wav", b"RIFF-wav", None), "galaius-media/speech-ab12.wav"),
    "music": (lambda cap: tools_media.generate_music("calm lo-fi piano, 1 minute", max_cost_usd=cap), ("music-ab12.mp3", b"ID3-mp3", None), "galaius-media/music-ab12.mp3"),
    "sound": (lambda cap: tools_media.generate_sound("rain on a tin roof", seconds=4, loop=True, max_cost_usd=cap), ("sound-ab12.wav", b"RIFF-wav", None), "galaius-media/sound-ab12.wav"),
}


@pytest.mark.parametrize(("ask", "made", "saved"), JOBS.values(), ids=JOBS.keys())
def test_a_media_tool_says_its_price_first_then_keeps_what_it_made_for_the_chat(here, monkeypatch, ask, made, saved):
    (here / "photo.png").write_bytes(PNG)
    platform = Platform(made)
    monkeypatch.setattr(ServerWorkspace, "configured", classmethod(lambda cls: platform))

    quoted = asyncio.run(ask(None))
    assert quoted.startswith("Quote: $0.3150") and "Nothing made, nothing charged" in quoted and not (here / "galaius-media").exists()

    said = asyncio.run(ask(0.315))
    path = here / saved
    assert path.read_bytes() == made[1] and str(path) in said and "$0.3150" in said and "Facturation" in said
    [(key, name)] = created_files(said)
    assert name == path.name and reg.RunFiles.path("maker", key).read_bytes() == made[1]  # the copy the chat downloads
    if made[2] is not None:
        assert said.endswith("\n\nHello")
    # Asked again the same way (a retried call): the same job, paid once (the same key), its file left as it was.
    assert asyncio.run(ask(0.315)) == said and platform.keys[-1] == platform.keys[-2] != platform.keys[0]


def test_a_paid_job_its_run_could_not_keep_is_never_asked(here, monkeypatch):
    register_run(run_id="reader", permission_mode="read_only")
    monkeypatch.setenv("GALAIUS_RUN_ID", "reader")
    platform = Platform(("translation-ab12.en.txt", b"Hello", "Hello"))
    monkeypatch.setattr(ServerWorkspace, "configured", classmethod(lambda cls: platform))
    assert asyncio.run(tools_media.translate("Bonjour", "en", max_cost_usd=1.0)).startswith(FILE_REFUSED + "this run reads only")
    assert platform.asked == []


def test_a_refusal_is_said_and_nothing_is_saved(here, monkeypatch):
    monkeypatch.setattr(ServerWorkspace, "configured", classmethod(lambda cls: Platform(("x.glb", MESH, None), "this job may cost up to $0.3150, past the $0.1000 asked (HTTP 402)")))
    said = asyncio.run(tools_media.translate("Bonjour", "en", max_cost_usd=0.1))
    assert said.startswith(FILE_REFUSED) and "past the $0.1000" in said and not (here / "galaius-media").exists()


def test_a_source_image_outside_the_working_folder_is_never_read(here, monkeypatch):
    platform = Platform(("x.glb", MESH, None))
    monkeypatch.setattr(ServerWorkspace, "configured", classmethod(lambda cls: platform))
    for image in ("../outside.png", "missing.png"):
        assert asyncio.run(tools_media.generate_3d(image)).startswith(FILE_REFUSED)
    assert platform.asked == []


def test_a_fenced_run_sends_no_image_from_a_folder_it_may_not_read(here, monkeypatch):
    from galaius.fence import FenceSpec

    (here / "work").mkdir()
    (here / "private").mkdir()
    (here / "private" / "scan.png").write_bytes(PNG)
    register_run(run_id="fenced", fence=FenceSpec(working_directory=here, levels={"work": "write", "private": "hidden"}, start=here / "work", state=here / "state"))
    monkeypatch.setenv("GALAIUS_RUN_ID", "fenced")
    platform = Platform(("x.glb", MESH, None))
    monkeypatch.setattr(ServerWorkspace, "configured", classmethod(lambda cls: platform))
    said = asyncio.run(tools_media.generate_3d("private/scan.png", max_cost_usd=1.0))
    assert said.startswith(FILE_REFUSED) and "hidden" in said and platform.asked == []
