"""Post-set verification: does the just-persisted key or model actually work?

``interact config set`` prints a verdict by running a real, tiny vision call
through the exact credential or model the user just saved — the one moment the
user is watching the answer, and the only moment a wrong key costs zero
confusion. A key saved silently sits in ``config.env`` until the first real
screenshot fails deep inside a vendor error.
"""

from __future__ import annotations

import asyncio
import os
import struct
import zlib

from interact.criteria import Criteria, CriteriaError
from interact.models import Model


class Verdict:
    """The probe's answer: does the just-set value work, and how do we know."""

    def __init__(self, ok: bool, model: str, detail: str, elapsed: float) -> None:
        self.ok = ok
        self.model = model
        self.detail = detail
        self.elapsed = elapsed


class ConfigCheck:
    """Everything the post-set verdict needs, in one place.

    One probe: a tiny in-memory PNG, one ``analyze_media`` call. One resolver:
    the model to probe a credential with comes from the user's OWN criterion
    (``media.criteria``) restricted to the provider that key belongs to — the
    same mechanism every other selection surface already reads, never a second
    literal model table.

    Class-level (no instances): the CLI calls it from a sync command context
    with no state worth carrying, and the built PNG is cached on the class so
    tests can reset one piece without touching module globals.
    """

    #: The probe prompt — a real (if tiny) analysis, cheap enough on every set.
    QUERY = "Reply with one short sentence: what does this image show?"

    _png: bytes | None = None

    @classmethod
    def reset(cls) -> None:
        cls._png = None

    @classmethod
    def image(cls) -> bytes:
        """A minimal valid 4x4 red PNG, built once per process — no fixture file."""
        if cls._png is not None:
            return cls._png
        width = height = 4
        row = b"\x00" + b"\xff\x00\x00" * width  # filter byte + one red row
        raw = row * height

        def chunk(tag: bytes, payload: bytes) -> bytes:
            crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
            return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        cls._png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        return cls._png

    @staticmethod
    def run(awaitable):
        """Bridge an async probe into the CLI's sync command context."""
        return asyncio.run(awaitable)

    @classmethod
    def _criterion_for(cls, env_name: str) -> str | None:
        """The model to probe with: the user's OWN image-role criterion, restricted
        (``runnable``) to the providers whose declared envKeys contain this key, so a set OPENAI
        key is verified by an openai model the criterion already prefers. Nothing qualifies →
        None → no probe, never an invented model id."""
        from interact.runtime import config as live

        providers = [
            name
            for name, keys in Model._provider_keys.items()
            if env_name in (keys or [])
        ]
        if not providers:
            return None
        try:
            criterion = Criteria.parse(live.criteria_for("image"))
        except CriteriaError:
            return None
        chosen = criterion.choose(
            available_only=True,
            runnable=lambda m: m.provider in providers,
            weights=live.criteria_weights,
        )
        return chosen.id if chosen else None

    @classmethod
    def _analyze(cls, model: str, what: str):
        """The single VLM call, split out for tests to patch."""
        from interact.runtime import config as live
        from interact.vision.core import analyze_media
        from interact.vision.types import MediaItem

        # The CLI process env can hold a STALE copy of a key (inherited from a
        # shell that sourced an older/corrupt config.env; dotenv loads with
        # override=False so it never corrects it). The file is the source of
        # truth — refresh so the probe judges what was JUST SAVED, not what the
        # shell happened to import. A probe that verifies the wrong value
        # reports a working key as broken (and vice versa).
        live.refresh()
        return analyze_media(
            [MediaItem.from_bytes(cls.image(), "image", "image/png")],
            f"A tiny test image, shown to verify {what} works.",
            live,
            cls.QUERY,
            model=model,
            role="image",
        )

    @classmethod
    async def key(cls, env_name: str, value: str = "") -> Verdict:
        """Run one tiny vision call through a just-set credential."""
        model = cls._criterion_for(env_name)
        if model is None:
            return Verdict(False, "", f"no probe model for {env_name}", 0.0)
        if value:
            previous = os.environ.get(env_name)
            os.environ[env_name] = value
        try:
            result = await cls._analyze(model, f"the {env_name} credential")
            text = (result.text or "").strip()
            if (result.text or "").startswith("ERROR:"):
                return Verdict(False, result.model or model, result.text, 0.0)
            return Verdict(True, result.model or model, text[:120], result.elapsed)
        except Exception as exc:  # a wrong key surfaces as ANY vendor error shape
            return Verdict(False, model, f"{type(exc).__name__}: {exc}", 0.0)
        finally:
            if value:
                if previous is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = previous

    @classmethod
    async def model(cls, model_id: str) -> Verdict:
        """Run one tiny vision call through a just-pinned model id."""
        try:
            result = await cls._analyze(model_id, "the model pin")
        except Exception as exc:
            return Verdict(False, model_id, f"{type(exc).__name__}: {exc}", 0.0)
        if (result.text or "").startswith("ERROR:"):
            return Verdict(False, model_id, result.text, 0.0)
        return Verdict(True, result.model or model_id, (result.text or "")[:120], result.elapsed)

    @staticmethod
    def line(verdict: Verdict) -> str:
        if verdict.ok:
            return (f"  ✓ it works — {verdict.model} read the test image "
                    f"in {verdict.elapsed:.1f}s")
        return f"  ⚠ it does NOT work ({verdict.model}): {verdict.detail}"