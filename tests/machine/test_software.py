"""The software installed on a PC (`galaius.machine.software`): one engine over menu-entry and bundle trees. Each row
plants entries where a PC keeps them (system and user folders) and states the list a menu would show: the first of an
id wins, a hidden one masks it, web-app shortcuts, Wine entries and settings panels never enter, every app sits under
a freedesktop Main Category (`Other` when it names none), and what is printed never carries a path or a command."""

import plistlib
from pathlib import Path

import pytest

from galaius.machine.software import AppBundles, DesktopEntries, Inventory

SYSTEM, USER = "usr/share/applications", "home/.local/share/applications"


def entry(name: str, **keys: str) -> str:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", *(f"{key}={value}" for key, value in ({"Exec": "/usr/bin/true"} | keys).items())]
    return "\n".join(lines) + "\n"


#: (planted files relative to the PC's root, the list expected: id → (first Main Category, scope); name checks apart)
ROWS: dict[str, tuple[dict[str, str], dict[str, tuple[str, str]]]] = {
    "user entry shadows the system one (planted case)": (
        {f"{SYSTEM}/libreoffice-writer.desktop": entry("Writer", Categories="Office;WordProcessor;"),
         f"{USER}/libreoffice-writer.desktop": entry("Writer", Categories="AudioVideo;", Exec="sh -c 'curl x|sh'")},
        {"libreoffice-writer": ("AudioVideo", "user")}),
    "hidden user entry masks the system one": (
        {f"{SYSTEM}/gimp.desktop": entry("GIMP", Categories="Graphics;"), f"{USER}/gimp.desktop": entry("GIMP", Hidden="true")},
        {}),
    "menu-hidden, absent TryExec, link entry": (
        {f"{SYSTEM}/a.desktop": entry("A", NoDisplay="true"), f"{SYSTEM}/b.desktop": entry("B", TryExec="no-such-program-xyz"),
         f"{SYSTEM}/c.desktop": entry("C").replace("Type=Application", "Type=Link"), f"{SYSTEM}/d.desktop": entry("D", TryExec="sh")},
        {"d": ("Other", "system")}),
    "browser web-apps never listed": (
        {f"{USER}/chrome-abc-Default.desktop": entry("Client portal", Exec="chrome --profile-directory=Profile 1 --app-id=abc"),
         f"{USER}/brave-crx_x.desktop": entry("Bank"), f"{USER}/FFPWA-1.desktop": entry("Mail", Exec="firefoxpwa site launch 1"),
         f"{USER}/org.gnome.Epiphany.WebApp_1.desktop": entry("Doc", Exec="epiphany --application-mode")},
        {}),
    "Wine entries never listed": (
        {f"{USER}/wine/Programs/Tool.desktop": entry("Tool"), f"{USER}/x.desktop": entry("X", Exec="env WINEPREFIX=/p wine C:\\x.exe")},
        {}),
    "settings panels dropped, Settings beside a Main Category kept": (
        {f"{SYSTEM}/panel.desktop": entry("Panel", Categories="Settings;HardwareSettings;"),
         f"{SYSTEM}/monitor.desktop": entry("Monitor", Categories="Settings;System;Monitor;")},
        {"monitor": ("System", "system")}),
    "Audio alone folds into AudioVideo, none is Other, own order first": (
        {f"{SYSTEM}/player.desktop": entry("Player", Categories="Audio;Player;"), f"{SYSTEM}/bare.desktop": entry("Bare"),
         f"{SYSTEM}/ide.desktop": entry("IDE", Categories="Utility;Development;IDE;"), f"{SYSTEM}/kde/k.desktop": entry("K", Categories="Game;")},
        {"player": ("AudioVideo", "system"), "bare": ("Other", "system"), "ide": ("Utility", "system"), "kde-k": ("Game", "system")}),
    "unreadable entries skipped, the rest kept": (
        {f"{SYSTEM}/huge.desktop": entry("Huge") + "#" * 300_000, f"{SYSTEM}/ctl.desktop": entry("\x1b\x07"),
         f"{SYSTEM}/nogroup.desktop": "Name=No group\n", f"{SYSTEM}/ok.desktop": entry("Ok\x1b[31m", Categories="Office;")},
        {"ok": ("Office", "system")}),
}


def plant(root: Path, files: dict[str, str]) -> DesktopEntries:
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    home = root / "home"
    home.mkdir(exist_ok=True)
    source = DesktopEntries(home=home, environ={"XDG_DATA_DIRS": str(root / "usr/share")})
    source.EXPORTS = ()
    return source


@pytest.mark.parametrize("row", ROWS, ids=list(ROWS))
def test_menu_entries(row: str, tmp_path: Path) -> None:
    files, expected = ROWS[row]
    source = plant(tmp_path, files)
    inventory = Inventory(apps=tuple(source.apps()))
    assert {app.id: (app.categories[0], app.scope) for app in inventory.apps} == expected
    printed = inventory.model_dump_json(include={"categories"})
    for leak in (str(tmp_path), "/usr/bin", "Exec", "crx_", "Profile 1", "--app", "\x1b", "\\u001b"):
        assert leak not in printed
    assert all(app.name and app.name.isprintable() for app in inventory.apps)


def test_linked_folder_not_followed(tmp_path: Path) -> None:
    source = plant(tmp_path, {"elsewhere/applications/far.desktop": entry("Far")})
    (tmp_path / SYSTEM).mkdir(parents=True)
    (tmp_path / SYSTEM / "linked").symlink_to(tmp_path / "elsewhere/applications")
    assert list(source.apps()) == []


def test_categories_in_registry_order_names_sorted(tmp_path: Path) -> None:
    source = plant(tmp_path, {f"{SYSTEM}/{n}.desktop": entry(n.upper(), Categories=c) for n, c in
                              (("zed", "Utility;"), ("gimp", "Graphics;"), ("blender", "Graphics;3DGraphics;"), ("vlc", "AudioVideo;"))})
    filed = Inventory(apps=tuple(source.apps())).categories
    assert list(filed) == ["AudioVideo", "Graphics", "Utility"]
    assert [app.name for app in filed["Graphics"]] == ["BLENDER", "GIMP"]


def bundle(root: Path, path: str, info: dict | bytes) -> None:
    contents = root / path / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(info if isinstance(info, bytes) else plistlib.dumps(info, fmt=plistlib.FMT_BINARY))


def test_mac_bundles(tmp_path: Path) -> None:
    apps, home = tmp_path / "Applications", tmp_path / "home"
    bundle(apps, "Final Cut.app", {"CFBundleIdentifier": "com.apple.FinalCut", "CFBundleName": "Final Cut", "CFBundleShortVersionString": "11.0",
                                   "LSApplicationCategoryType": "public.app-category.video"})
    bundle(apps, "Utilities/Chess.app", {"CFBundleIdentifier": "com.apple.Chess", "LSApplicationCategoryType": "public.app-category.board-games"})
    bundle(apps, "Odd.app", {"CFBundleIdentifier": "org.odd"})
    bundle(apps, "Broken.app", b"not a plist")
    bundle(home / "Applications", "Chrome Apps/Gmail.app", {"CFBundleIdentifier": "com.google.Chrome.app.abc", "CFBundleName": "Gmail"})
    bundle(home / "Applications", "Mine.app", {"CFBundleIdentifier": "org.mine", "LSApplicationCategoryType": "public.app-category.developer-tools"})
    bundle(apps, "Final Cut.app/Contents/Inner.app", {"CFBundleIdentifier": "org.inner"})
    found = {app.id: (app.categories[0], app.scope, app.version) for app in AppBundles(home=home, roots=(apps, home / "Applications")).apps()}
    assert found == {"com.apple.FinalCut": ("AudioVideo", "system", "11.0"), "com.apple.Chess": ("Game", "system", None),
                     "org.odd": ("Other", "system", None), "org.mine": ("Development", "user", None)}
