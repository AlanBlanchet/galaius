"""An agent's picture comes from the platform, never from a provider key on this computer: the
`generate_image` tool saves what the platform drew (charged there, linked to the asking run), and an
agent CLI started by galaius holds no model API key but its own login's."""

import asyncio
import base64
import json
from uuid import uuid4

import pytest
from galaius_core import GeneratedImage, ImageAnswer

from galaius.agents.providers import PROVIDERS
from galaius.server import tools_images
from galaius.server_workspace import ImagesRefused, ServerWorkspace

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")


class Platform:
    """Stands in for the server's `POST /images` at the one client call."""

    def __init__(self, refusal: str | None = None) -> None:
        self.refusal, self.asked = refusal, []

    def draw_images(self, request, idempotency_key):
        self.asked.append((request, idempotency_key))
        if self.refusal:
            raise ImagesRefused(self.refusal)
        return ImageAnswer(provider="openai", model=request.model, cost_usd=0.0336,
                           images=(GeneratedImage(run_id=uuid4(), media_type="image/png", data_base64=base64.b64encode(PNG).decode(), size_bytes=len(PNG), cost_usd=0.0336),))


@pytest.fixture
def platform(monkeypatch, tmp_path):
    served = Platform()
    monkeypatch.setattr(ServerWorkspace, "configured", classmethod(lambda cls: served))
    monkeypatch.chdir(tmp_path)
    return served


def test_the_tool_saves_what_the_platform_drew_and_names_the_asking_run(platform, monkeypatch, tmp_path):
    run = uuid4()
    monkeypatch.setenv("GALAIUS_RUN_ID", str(run))
    said = asyncio.run(tools_images.generate_image("a red fox, flat", background="transparent", folder="art"))
    (saved,) = (tmp_path / "art").iterdir()
    assert saved.read_bytes() == PNG and str(saved) in said and "$0.0336" in said and "Facturation" in said
    request, key = platform.asked[0]
    assert (request.caller_run, request.background, request.prompt) == (run, "transparent", "a red fox, flat") and key


def test_a_refusal_is_said_and_nothing_is_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(ServerWorkspace, "configured", classmethod(lambda cls: Platform("this month already cost $10.02 of a $10.00 ceiling (HTTP 402)")))
    monkeypatch.chdir(tmp_path)
    said = asyncio.run(tools_images.generate_image("a fox"))
    assert said.startswith("No image drawn:") and "ceiling" in said and not list(tmp_path.rglob("image-*"))


@pytest.mark.parametrize(("provider", "codex_login", "kept", "gone"), [
    ("claude", None, {"ANTHROPIC_API_KEY", "GALAIUS_OPENAI_COMPAT_KEY", "GALAIUS_RUN_ID", "GH_TOKEN"}, {"OPENAI_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY"}),
    ("codex", "chatgpt", {"GALAIUS_OPENAI_COMPAT_KEY"}, {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"}),   # its login is ChatGPT: no key read
    ("codex", "apikey", {"OPENAI_API_KEY"}, {"ANTHROPIC_API_KEY", "GEMINI_API_KEY"}),                                 # its login IS that key
])
def test_an_agent_holds_no_model_key_but_its_own_logins(tmp_path, provider, codex_login, kept, gone):
    home = tmp_path / ".codex"
    home.mkdir()
    if codex_login:
        (home / "auth.json").write_text(json.dumps({"auth_mode": codex_login, "OPENAI_API_KEY": None}))
    environment = {name: "x" for name in kept | gone} | {"CODEX_HOME": str(home), "PATH": "/usr/bin"}
    child = PROVIDERS[provider].child_environment(environment)
    assert kept <= child.keys() and not gone & child.keys()
    # Codex's app server starts from its subscription environment: no key of any kind reaches it.
    assert not {"OPENAI_API_KEY", "GEMINI_API_KEY"} & PROVIDERS["codex"].subscription_env(environment).keys()


def test_the_tool_never_saves_outside_the_working_folder(platform, tmp_path):
    said = asyncio.run(tools_images.generate_image("a fox", folder="../elsewhere"))
    assert said.startswith("No image drawn:") and "outside" in said and platform.asked == []
