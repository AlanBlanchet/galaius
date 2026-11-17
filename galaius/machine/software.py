"""The software installed on this PC, read here and kept here (`galaius machine software`): each launchable application
under one of the freedesktop Main Categories, the standard a Linux menu files apps by
(https://specifications.freedesktop.org/menu/latest/category-registry.html), plus the catch-all that standard asks for.
macOS's own `LSApplicationCategoryType` values fold into it (`APPLE_CATEGORIES`). Nothing here is sent anywhere.

What never enters the list: entries a menu hides (`NoDisplay`, `Hidden`, a `TryExec` program absent), browser web-app
shortcuts (their name is a site he visited, their command a browser profile), Wine-generated entries, OS settings panels.
What never leaves this module: paths, commands, icons — an app is its id, name, version, categories and whether it is
installed for the whole system or for this user only."""

import os
import plistlib
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, field_validator

#: The freedesktop Main Categories, in the registry's order, without `Settings` (OS panels, never an app he means) and
#: with `Audio` / `Video` folded into `AudioVideo` (the registry requires them together), then the catch-all.
MAIN_CATEGORIES: tuple[str, ...] = ("AudioVideo", "Development", "Education", "HealthFitness", "Game", "Graphics", "Network",
                                    "Office", "Science", "System", "Utility", "Other")
#: Registered names that file an app under another Main Category.
FOLDED: dict[str, str] = {"Audio": "AudioVideo", "Video": "AudioVideo"}
#: Every `public.app-category.*` value of LSApplicationCategoryType
#: (https://developer.apple.com/documentation/bundleresources/information-property-list/lsapplicationcategorytype) →
#: its Main Category; a `*-games` value is a Game.
APPLE_CATEGORIES: dict[str, str] = {
    "video": "AudioVideo", "music": "AudioVideo", "entertainment": "AudioVideo",
    "graphics-design": "Graphics", "photography": "Graphics",
    "developer-tools": "Development",
    "productivity": "Office", "business": "Office", "finance": "Office",
    "social-networking": "Network", "news": "Network", "weather": "Network", "travel": "Network",
    "medical": "Science",
    "education": "Education", "reference": "Education",
    "healthcare-fitness": "HealthFitness", "sports": "HealthFitness", "lifestyle": "HealthFitness",
    "games": "Game", "utilities": "Utility",
}
#: A command or id that marks a browser web-app shortcut: Chromium browsers (`crx_`, `--app-id`, `--app=`), Firefox PWAs,
#: GNOME Web (`--application-mode`, `WebApp_`), Chrome and Safari on macOS.
WEB_APP = re.compile(r"crx_|--app-id|--app=|firefoxpwa|--application-mode|\.WebApp_|^com\.google\.Chrome\.app\.|^com\.apple\.Safari\.WebApp\.")
#: A command or id Wine generated for a Windows program (its entries live under `applications/wine/`: id `wine-…`).
WINE = re.compile(r"(^|[/\s\"'])wine(64)?(\s|$)|\.wine/|^wine-", re.IGNORECASE)
#: Characters a name never prints: a terminal reads C0 / C1 controls as commands.
CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: Largest entry file read (a real one is a few kB; a larger one is not a menu entry).
ENTRY_BYTES = 256 * 1024


class InstalledApp(BaseModel):
    """One application as the list keeps it: no path, command or icon, ever."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    #: Its menu name, control characters dropped (a terminal reads them as commands), at most 120 characters.
    name: str = Field(min_length=1)
    #: The app's own version where the OS records it beside the app (macOS bundles); None on Linux menu entries, whose
    #: `Version` key is the entry format's version, not the app's.
    version: str | None = None
    #: Its Main Categories, its own order first, `Other` when it names none.
    categories: tuple[str, ...]
    #: `user`: installed in his home folder (his own, can name anything); `system`: for every account of this PC.
    scope: Literal["system", "user"]

    @field_validator("name", mode="before")
    @classmethod
    def _printable(cls, name: str) -> str:
        return CONTROLS.sub("", str(name)).strip()[:120]


class Inventory(BaseModel):
    """Every application of this PC, filed by Main Category."""

    apps: tuple[InstalledApp, ...]

    @computed_field
    @property
    def categories(self) -> dict[str, list[InstalledApp]]:
        """Each app under its first Main Category, categories in the registry's order, names sorted; empty ones absent."""
        filed: dict[str, list[InstalledApp]] = {category: [] for category in MAIN_CATEGORIES}
        for app in self.apps:
            filed[app.categories[0]].append(app)
        return {category: sorted(apps, key=lambda app: app.name.casefold()) for category, apps in filed.items() if apps}

    @classmethod
    def read(cls) -> "Inventory":
        """This PC's applications; NotImplementedError where its OS has no reader yet."""
        source = {"linux": DesktopEntries, "darwin": AppBundles}.get(sys.platform)
        if source is None:
            raise NotImplementedError(f"installed software is not read on {sys.platform} yet (Linux and macOS are)")
        return cls(apps=tuple(source().apps()))


def main_categories(names: list[str]) -> tuple[str, ...]:
    """The Main Categories among `names`, its order kept, each once; `("Other",)` when none."""
    found = dict.fromkeys(FOLDED.get(name, name) for name in names)
    return tuple(name for name in found if name in MAIN_CATEGORIES and name != "Other") or ("Other",)


class DesktopEntries:
    """Linux: the menu's `.desktop` entries (https://specifications.freedesktop.org/desktop-entry/latest/) in
    `$XDG_DATA_HOME` then `$XDG_DATA_DIRS` `applications/`, the first of an id winning, as a menu does; Flatpak and Snap
    export theirs there (their export folders are added when the environment lacks them, as under a service)."""

    EXPORTS: ClassVar[tuple[str, ...]] = ("/var/lib/flatpak/exports/share", "/var/lib/snapd/desktop")

    def __init__(self, home: Path | None = None, environ: dict[str, str] | None = None) -> None:
        self.home = home or Path.home()
        self.environ = os.environ if environ is None else environ

    def roots(self) -> list[Path]:
        data_home = self.environ.get("XDG_DATA_HOME") or str(self.home / ".local/share")
        data_dirs = (self.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
        user_flatpak = str(self.home / ".local/share/flatpak/exports/share")
        listed = [data_home, user_flatpak, *data_dirs, *self.EXPORTS]
        return [Path(path) / "applications" for path in dict.fromkeys(path for path in listed if os.path.isabs(path))]

    def apps(self) -> Iterator[InstalledApp]:
        seen: set[str] = set()
        for root in self.roots():
            for path, entry_id in self._entries(root):
                if entry_id in seen:
                    continue
                seen.add(entry_id)  # a hidden or refused entry still masks the same id further down, as in a menu
                try:
                    app = self._app(path, entry_id)
                except ValidationError:
                    continue  # one unreadable entry never ends the list
                if app is not None:
                    yield app

    @staticmethod
    def _entries(root: Path) -> Iterator[tuple[Path, str]]:
        """Each `.desktop` file under `root` and its id (its relative path, `/` → `-`); links to folders not followed."""
        for folder, folders, files in os.walk(root, followlinks=False):
            folders.sort()
            for name in sorted(files):
                if name.endswith(".desktop"):
                    path = Path(folder) / name
                    yield path, path.relative_to(root).as_posix().replace("/", "-")

    def _app(self, path: Path, entry_id: str) -> InstalledApp | None:
        entry = self._group(path)
        if entry is None or entry.get("Type") != "Application" or entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true":
            return None
        name, command = entry.get("Name", "").strip(), entry.get("Exec", "")
        if not name or WEB_APP.search(entry_id) or WEB_APP.search(command) or WINE.search(command) or WINE.search(entry_id):
            return None
        try_exec = entry.get("TryExec")
        if try_exec and shutil.which(try_exec) is None:
            return None
        names = [name for name in entry.get("Categories", "").split(";") if name]
        if names and set(main_categories(names)) == {"Other"} and "Settings" in names:
            return None  # an OS settings panel
        scope: Literal["system", "user"] = "user" if path.is_relative_to(self.home) else "system"
        return InstalledApp(name=name, id=entry_id.removesuffix(".desktop"), categories=main_categories(names), scope=scope)

    @staticmethod
    def _group(path: Path) -> dict[str, str] | None:
        """The `[Desktop Entry]` group's unlocalized keys; None when the file cannot be read as one."""
        try:
            with path.open("rb") as file:
                raw = file.read(ENTRY_BYTES + 1)
        except OSError:
            return None
        if len(raw) > ENTRY_BYTES:
            return None
        keys: dict[str, str] = {}
        inside = False
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                if inside:
                    break
                inside = line == "[Desktop Entry]"
            elif inside and "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                keys.setdefault(key.strip(), value.strip())
        return keys or None


class AppBundles:
    """macOS: the `.app` bundles of `/Applications` (and one folder below), `/System/Applications` and
    `~/Applications`, each read from its `Contents/Info.plist`."""

    def __init__(self, home: Path | None = None, roots: tuple[Path, ...] | None = None) -> None:
        self.home = home or Path.home()
        self.roots = roots or (Path("/Applications"), Path("/System/Applications"), self.home / "Applications")

    def apps(self) -> Iterator[InstalledApp]:
        seen: set[str] = set()
        for root in self.roots:
            for bundle in self._bundles(root):
                try:
                    app = self._app(bundle)
                except ValidationError:
                    continue
                if app is not None and app.id not in seen:
                    seen.add(app.id)
                    yield app

    @staticmethod
    def _bundles(root: Path) -> Iterator[Path]:
        """`*.app` directly in `root` or in one folder of it (Utilities, a vendor's folder); never inside a bundle."""
        try:
            children = sorted(root.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            if child.suffix == ".app":
                yield child
                continue
            try:
                yield from sorted(grand for grand in child.iterdir() if grand.suffix == ".app" and grand.is_dir() and not grand.is_symlink())
            except OSError:
                continue

    def _app(self, bundle: Path) -> InstalledApp | None:
        try:
            with (bundle / "Contents" / "Info.plist").open("rb") as file:
                info = plistlib.load(file)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        if not isinstance(info, dict):
            return None
        bundle_id = str(info.get("CFBundleIdentifier") or bundle.stem)
        if WEB_APP.search(bundle_id):
            return None
        name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle.stem)
        version = info.get("CFBundleShortVersionString")
        kind = str(info.get("LSApplicationCategoryType") or "").removeprefix("public.app-category.")
        category = "Game" if kind.endswith("-games") else APPLE_CATEGORIES.get(kind, "Other")
        scope: Literal["system", "user"] = "user" if bundle.is_relative_to(self.home) else "system"
        return InstalledApp(name=name, id=bundle_id, version=str(version)[:40] if version else None, categories=(category,), scope=scope)
