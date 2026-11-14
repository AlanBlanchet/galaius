"""What the tray icon shows for one `Presence`: its picture, its tooltip and its menu rows, the same on
every system (each backend in this package only puts them on screen)."""

import ctypes
import locale
import os
import sys
from functools import cache
from typing import ClassVar, Literal, Protocol

import numpy as np
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict

from galaius.machine.presence import Presence, Shown

#: What a menu row does when clicked (None: a row that only says something).
TrayAction = Literal["open", "pause", "resume", "start", "quit"]
#: The words, by language: the computer's own (`TrayWords.current`).
Language = Literal["fr", "en"]


class TrayDriver(Protocol):
    """What a backend drives (`galaius.machine.tray.app.TrayApp`)."""

    leaving: bool

    def tick(self) -> "TrayView | None": ...

    def act(self, action: TrayAction) -> None: ...


class TrayBackend:
    """Puts a `TrayView` on this system's tray and calls back on its menu: one per operating system.
    Every call to it, and every call it makes, happens on its own UI thread."""

    def run(self, driver: TrayDriver) -> None:
        """Shows the icon, calls `driver.tick()` every `TrayView.frame_seconds` (showing what it returns)
        and `driver.act` for a clicked row, until `driver.leaving`."""
        raise NotImplementedError

    def open(self, url: str) -> None:
        """`url` in the person's browser."""
        raise NotImplementedError


class TrayWords(BaseModel):
    """Every word the icon shows, in one language."""

    model_config = ConfigDict(frozen=True)
    shown: dict[Shown, str]
    #: « working » with its run count: (one run, several runs).
    working: tuple[str, str]
    actions: dict[TrayAction, str]
    update: str

    #: The French words, then the English ones.
    by_language: ClassVar[dict[Language, "TrayWords"]]

    @classmethod
    def current(cls) -> "TrayWords":
        """The computer's language: Windows' display language, else the locale the process runs in."""
        if sys.platform == "win32":
            french = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0C  # LANG_FRENCH, every French variant
        else:
            names = (os.environ.get("LC_ALL"), os.environ.get("LC_MESSAGES"), os.environ.get("LANG"), locale.getlocale()[0])
            french = any(name and name.lower().startswith("fr") for name in names)
        return cls.by_language["fr" if french else "en"]

    def state(self, presence: Presence, shown: Shown) -> str:
        if shown == "working":
            single, several = self.working
            return (single if presence.runs == 1 else several).format(n=presence.runs)
        return self.shown[shown]


TrayWords.by_language = {
    "fr": TrayWords(shown={"connected": "Connecté", "working": "", "reconnecting": "Reconnexion…", "paused": "En pause", "refused": "À reconnecter", "off": "Éteint"},
                    working=("{n} agent actif", "{n} agents actifs"), update="Mise à jour après les agents",
                    actions={"open": "Ouvrir Galaius", "pause": "Mettre en pause", "resume": "Reprendre", "start": "Démarrer", "quit": "Quitter"}),
    "en": TrayWords(shown={"connected": "Connected", "working": "", "reconnecting": "Reconnecting…", "paused": "Paused", "refused": "Sign in again", "off": "Off"},
                    working=("{n} agent working", "{n} agents working"), update="Update after the agents",
                    actions={"open": "Open Galaius", "pause": "Pause", "resume": "Resume", "start": "Start", "quit": "Quit"}),
}


class TrayItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    label: str
    action: TrayAction | None = None


class TrayView(BaseModel):
    """One frame of the icon: the picture (`picture`), its tooltip and its menu."""

    model_config = ConfigDict(frozen=True)
    shown: Shown
    frame: int = 0
    tooltip: str
    items: tuple[TrayItem, ...]

    #: The working icon's pulse: this many frames, each `frame_seconds` (a 2.4 s loop, the web's beat).
    frames: ClassVar[int] = 6
    frame_seconds: ClassVar[float] = 0.4

    @classmethod
    def of(cls, presence: Presence, alive: bool, words: TrayWords, frame: int = 0) -> "TrayView":
        shown = presence.shown(alive)
        state = words.state(presence, shown)
        rows = [TrayItem(label=f"{presence.pc} · {state}" if presence.pc else state)]
        if presence.page:
            rows.append(TrayItem(label=words.actions["open"], action="open"))
        if shown == "off":
            rows.append(TrayItem(label=words.actions["start"], action="start"))
        elif shown == "paused":
            rows.append(TrayItem(label=words.actions["resume"], action="resume"))
        elif shown != "refused":
            rows.append(TrayItem(label=words.actions["pause"], action="pause"))
        if alive and presence.update_waiting:
            rows.append(TrayItem(label=words.update))
        rows.append(TrayItem(label=words.actions["quit"], action="quit"))
        return cls(shown=shown, frame=frame % cls.frames if shown == "working" else 0, tooltip=f"Galaius · {state}", items=tuple(rows))

    def picture(self, size: int) -> Image.Image:
        return TrayPicture.drawn(self.shown, self.frame, size)


class TrayPicture:
    """The brand mark (the site's logo: a reticle ring, four ticks, the pupil, on its violet-to-cyan
    gradient) with a state dot at its lower right, drawn at any size from the logo's 64-unit grid."""

    #: The dot's colour per state; « off » also fades the mark (`faded`).
    dots: ClassVar[dict[Shown, tuple[int, int, int]]] = {
        "connected": (34, 197, 94), "working": (34, 197, 94), "reconnecting": (245, 158, 11),
        "paused": (148, 163, 184), "off": (148, 163, 184), "refused": (239, 68, 68)}
    gradient: ClassVar[tuple[tuple[float, tuple[int, int, int]], ...]] = ((0.0, (167, 139, 250)), (0.5, (139, 92, 246)), (1.0, (34, 211, 238)))
    faded: ClassVar[float] = 0.4
    #: Drawn this many times larger, then reduced: smooth edges at 16 px.
    supersample: ClassVar[int] = 4

    @staticmethod
    @cache
    def drawn(shown: Shown, frame: int, size: int) -> Image.Image:
        scale = size * TrayPicture.supersample / 64
        big = round(64 * scale)
        mask = Image.new("L", (big, big), 0)
        pen = ImageDraw.Draw(mask)
        width = round(5 * scale)
        pen.ellipse([(32 - 21 - 2.5) * scale, (32 - 21 - 2.5) * scale, (32 + 21 + 2.5) * scale, (32 + 21 + 2.5) * scale], outline=255, width=width)
        for start, end in (((32, 2.5), (32, 12)), ((32, 52), (32, 61.5)), ((2.5, 32), (12, 32)), ((52, 32), (61.5, 32))):
            pen.line([tuple(v * scale for v in start), tuple(v * scale for v in end)], fill=255, width=width)
            for x, y in (start, end):  # round caps
                pen.ellipse([(x - 2.5) * scale, (y - 2.5) * scale, (x + 2.5) * scale, (y + 2.5) * scale], fill=255)
        pen.ellipse([(32 - 8.5) * scale, (32 - 8.5) * scale, (32 + 8.5) * scale, (32 + 8.5) * scale], fill=255)
        mark = TrayPicture._painted(mask, big)
        if shown == "off":
            mark.putalpha(mark.getchannel("A").point(lambda alpha: round(alpha * TrayPicture.faded)))
        pen = ImageDraw.Draw(mark)
        centre, radius = 49 * scale, 11 * scale
        if shown == "working":  # a ring leaving the pupil and fading, one step per frame
            grown = (8.5 + (frame + 1) * 1.9) * scale
            ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse([32 * scale - grown, 32 * scale - grown, 32 * scale + grown, 32 * scale + grown],
                                         outline=(*TrayPicture.dots[shown], round(255 * (1 - frame / TrayView.frames))), width=round(2.5 * scale))
            mark = Image.alpha_composite(mark, ring)
            pen = ImageDraw.Draw(mark)
        rim = radius + 2.5 * scale
        pen.ellipse([centre - rim, centre - rim, centre + rim, centre + rim], fill=(255, 255, 255, 255))
        pen.ellipse([centre - radius, centre - radius, centre + radius, centre + radius], fill=(*TrayPicture.dots[shown], 255))
        return mark.resize((size, size), Image.Resampling.LANCZOS)

    @staticmethod
    def _painted(mask: Image.Image, big: int) -> Image.Image:
        """The mask's shape in the logo's diagonal gradient."""
        along = (np.add.outer(np.arange(big), np.arange(big)) / max(2 * big - 2, 1))[..., None]
        (a, first), (b, middle), (_, last) = TrayPicture.gradient
        low = np.array(first) + (np.array(middle) - np.array(first)) * (along / b)
        high = np.array(middle) + (np.array(last) - np.array(middle)) * ((along - b) / (1 - b))
        colour = np.where(along < b, low, high).clip(0, 255).astype(np.uint8)
        picture = Image.fromarray(colour, "RGB").convert("RGBA")
        picture.putalpha(mask)
        return picture

    @staticmethod
    @cache
    def argb(shown: Shown, frame: int, size: int) -> bytes:
        """The picture as 32-bit ARGB in network byte order (D-Bus StatusNotifierItem's `IconPixmap`)."""
        return np.asarray(TrayPicture.drawn(shown, frame, size).convert("RGBA"))[..., [3, 0, 1, 2]].tobytes()
