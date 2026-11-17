"""The software installed on this PC, read here and kept here (`galaius machine software`): each launchable application
under one of the freedesktop Main Categories, the standard a Linux menu files apps by
(https://specifications.freedesktop.org/menu/latest/category-registry.html), plus the catch-all that standard asks for.
macOS's own `LSApplicationCategoryType` values fold into it (`APPLE_CATEGORIES`). Nothing here is sent anywhere.

What never enters the list: entries a menu hides (`NoDisplay`, `Hidden`, a `TryExec` program absent), browser web-app
shortcuts (their name is a site he visited, their command a browser profile), Wine entries and Windows-VM launchers
(named after whatever Windows software he installed there), OS settings panels.
What never leaves this module: paths, commands, icons — an app is its id, name, version, categories and whether it is
installed for the whole system or for this user only."""

import os
import plistlib
import re
import shutil
import stat
import sys
import unicodedata
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
#: GNOME Web (`--application-mode`, `WebApp_`), Linux Mint's Web Apps (`webapp-…`, `--class WebApp-…`, `ice/` profiles),
#: Chromium-family app shims and Safari web apps on macOS (`<browser id>….app.…`, `com.apple.Safari.WebApp.…`).
WEB_APP = re.compile(r"crx_|--app-id|--app=|firefoxpwa|--application-mode|\bWebApp[-_]|/ice/|"
                     r"^(com\.google\.Chrome|com\.microsoft\.edgemac|com\.brave\.Browser|org\.chromium\.Chromium|com\.vivaldi\.Vivaldi|"
                     r"com\.operasoftware\.Opera)[\w.-]*?\.app\.|^com\.apple\.Safari\.WebApp\.", re.IGNORECASE)
#: Info.plist keys only a Chromium app shim carries (its site's profile and shortcut).
APP_SHIM_KEYS = ("CrAppModeShortcutID", "CrAppModeProfileDirName")
#: A command or id launching a Windows program: Wine's generated entries (under `applications/wine/`: id `wine-…`),
#: WinApps' Windows-VM launchers, Bottles' and PlayOnLinux's per-program launchers; their names are whatever Windows
#: software he installed there (Bottles and PlayOnLinux themselves stay listed).
WINDOWS = re.compile(r"(^|[/\s\"'])((wine(64)?|winapps)([\s\"']|$)|playonlinux4?[\"']?\s+--run)|\.wine/|^wine-|bottles-cli\b", re.IGNORECASE)
#: Vendor categories those launchers file themselves under.
WINDOWS_CATEGORIES = frozenset({"WinApps", "Wine"})
#: Largest entry or Info.plist read (a real one is a few kB).
ENTRY_BYTES = 256 * 1024


def printable(text: str) -> str:
    """`text` without Unicode's control and format characters (a terminal reads C0 / C1 controls as commands; a
    direction mark makes a name read backwards), at most 120 characters."""
    return "".join(char for char in text if unicodedata.category(char)[0] != "C").strip()[:120]


def regular_bytes(path: Path) -> bytes | None:
    """At most `ENTRY_BYTES` of `path` when it is a regular file (a link to one included); None for anything else — a
    FIFO would block the read for ever, a device never ends — or larger, or unreadable."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = file.read(ENTRY_BYTES + 1)
    return raw if len(raw) <= ENTRY_BYTES else None


class InstalledApp(BaseModel):
    """One application as the list keeps it: no path, command or icon, ever."""

    model_config = ConfigDict(frozen=True)

    #: Its menu entry's or bundle's id, and its menu name: both `printable`.
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    #: The app's own version where the OS records it beside the app (macOS bundles); None on Linux menu entries, whose
    #: `Version` key is the entry format's version, not the app's.
    version: str | None = None
    #: Its Main Categories, its own order first, `Other` when it names none.
    categories: tuple[str, ...]
    #: `user`: installed in his home folder (his own, can name anything); `system`: for every account of this PC.
    scope: Literal["system", "user"]

    @field_validator("id", "name", mode="before")
    @classmethod
    def _printable(cls, text: str) -> str:
        return printable(text)


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
        # A relative path in either variable is invalid and ignored (base-directory spec); none left → the default.
        data_home = [path for path in [self.environ.get("XDG_DATA_HOME", "")] if os.path.isabs(path)] or [str(self.home / ".local/share")]
        data_dirs = [path for path in self.environ.get("XDG_DATA_DIRS", "").split(":") if os.path.isabs(path)] or ["/usr/local/share", "/usr/share"]
        listed = [*data_home, str(self.home / ".local/share/flatpak/exports/share"), *data_dirs, *self.EXPORTS]
        return [Path(path) / "applications" for path in dict.fromkeys(listed)]

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
        names = [name.strip() for name in entry.get("Categories", "").split(";") if name.strip()]
        if not name or WEB_APP.search(entry_id) or WEB_APP.search(command) or WINDOWS.search(command) or WINDOWS.search(entry_id) \
                or WINDOWS_CATEGORIES.intersection(names):
            return None
        try_exec = entry.get("TryExec")
        if try_exec and shutil.which(try_exec) is None:
            return None
        if names and set(main_categories(names)) == {"Other"} and "Settings" in names:
            return None  # an OS settings panel
        scope: Literal["system", "user"] = "user" if path.is_relative_to(self.home) else "system"
        return InstalledApp(name=name, id=entry_id.removesuffix(".desktop"), categories=main_categories(names), scope=scope)

    @staticmethod
    def _group(path: Path) -> dict[str, str] | None:
        """The `[Desktop Entry]` group's unlocalized keys; None when the file cannot be read as one."""
        raw = regular_bytes(path)
        if raw is None:
            return None
        keys: dict[str, str] = {}
        inside = False
        for line in raw.decode("utf-8-sig", errors="replace").splitlines():
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
        raw = regular_bytes(bundle / "Contents" / "Info.plist")
        if raw is None:
            return None
        try:
            info = plistlib.loads(raw)
        except Exception:  # untrusted bytes: plistlib raises ValueError, AttributeError, ExpatError, OverflowError… per malformation
            return None
        if not isinstance(info, dict) or any(key in info for key in APP_SHIM_KEYS):
            return None
        text = {key: value for key, value in info.items() if isinstance(value, str) and value}  # a non-string value is absent
        bundle_id = text.get("CFBundleIdentifier", bundle.stem)
        if WEB_APP.search(bundle_id):
            return None
        name = text.get("CFBundleDisplayName") or text.get("CFBundleName") or bundle.stem
        version = text.get("CFBundleShortVersionString")
        kind = text.get("LSApplicationCategoryType", "").removeprefix("public.app-category.")
        category = "Game" if kind.endswith("-games") else APPLE_CATEGORIES.get(kind, "Other")
        scope: Literal["system", "user"] = "user" if bundle.is_relative_to(self.home) else "system"
        return InstalledApp(name=name, id=bundle_id, version=printable(version)[:40] or None if version else None, categories=(category,), scope=scope)
