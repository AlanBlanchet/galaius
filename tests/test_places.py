"""Levels per folder of a PC (`galaius.places.PlaceMap`): every folder starts hidden, a level holds
for the folder and everything beneath it until a deeper one, credential stores and links are never
opened whatever an ancestor says, and browsing yields names only, page by page."""

import sys
from pathlib import Path

import pytest

from galaius.places import NEVER_GRANTABLE, BrowseBudget, PlaceMap, split

pytestmark = pytest.mark.usefixtures("directory_backend")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for folder in ("projects/app/src", "projects/secret", "Documents", ".ssh", "interact-files"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "projects" / "app" / "README.md").write_text("app")
    (tmp_path / "projects" / "id_ed25519").write_text("key")
    (tmp_path / "projects" / ".env").write_text("SECRET=1")
    (tmp_path / "projects" / "to-ssh").symlink_to(tmp_path / ".ssh")
    return tmp_path


def test_every_folder_starts_hidden_and_a_level_holds_below_until_a_deeper_one(home: Path) -> None:
    places = PlaceMap(working_directory=home, levels={"projects": "read", "projects/secret": "hidden", "projects/app/src": "write"})
    assert places.level(()) == places.level(("Documents",)) == "hidden"
    assert places.level(("projects", "app")) == "read"
    assert places.level(("projects", "secret", "deep")) == "hidden"
    assert places.level(("projects", "app", "src", "x")) == "write"


@pytest.mark.parametrize(("parts", "platform", "reason"), [
    ((".ssh",), "linux", "hidden"),
    (("projects", ".git"), "linux", "hidden"),
    (("projects", "ID_ED25519"), "linux", "credential"),  # casefolded
    (("vault.KDBX",), "linux", "credential"),
    (("AppData", "Roaming"), "win32", "credential and startup"),
    (("appdata",), "win32", "credential and startup"),
    (("Library", "Keychains"), "darwin", "credential"),
    (("library", "launchagents", "x.plist"), "darwin", "credential and startup"),
])
def test_credential_stores_are_never_grantable_on_any_os(parts, platform, reason) -> None:
    assert reason in NEVER_GRANTABLE.refusal(parts, parts, platform)


def test_ordinary_folders_are_grantable_on_every_os() -> None:
    for platform in ("linux", "darwin", "win32"):
        assert NEVER_GRANTABLE.refusal(("Documents", "Library notes"), ("Documents", "Library notes"), platform) is None


@pytest.mark.parametrize(("path", "reason"), [
    ("projects/to-ssh", "is a link"),
    ("projects/to-ssh/config", "is a link"),
    ("projects/id_ed25519", "credential"),
    (".ssh", "hidden"),
])
def test_a_level_is_refused_on_a_walked_link_or_a_never_grantable_part(home: Path, path: str, reason: str) -> None:
    places = PlaceMap(working_directory=home)
    with pytest.raises(PermissionError, match=reason):
        places.with_level(path, "read")
    assert places.with_level(path, "hidden") == {path: "hidden"} or places.level(split(path)) == "hidden"


def test_a_level_under_a_refused_folder_is_not_in_force(home: Path) -> None:
    places = PlaceMap(working_directory=home, levels={"projects/to-ssh": "read", "projects": "read"})
    assert "is a link" in {place.path: place.refused for place in places.entries()}["projects/to-ssh"]
    assert places.reach(("projects", "to-ssh")) == "hidden"
    assert list(places.in_force()) == ["projects"]


@pytest.mark.parametrize(("start", "path", "level", "widens"), [
    ({}, "projects", "see", True),
    ({"projects": "read"}, "projects", "see", False),
    ({"projects": "read"}, "projects/app", "write", True),
    ({"projects": "write"}, "projects/app", "sandbox", False),
    ({"projects": "sandbox"}, "projects", "write", True),
    ({"projects": "write_on_review"}, "projects", "write", True),
    ({"projects": "write"}, "projects", "write_on_review", False),
    ({"projects": "read"}, "projects", "hidden", False),
])
def test_widening_means_a_later_level_than_the_one_in_force_now(home: Path, start, path, level, widens) -> None:
    assert PlaceMap(working_directory=home, levels=start).widens(path, level) is widens


def test_setting_the_inherited_level_drops_the_entry(home: Path) -> None:
    places = PlaceMap(working_directory=home, levels={"projects": "read", "projects/app": "write"})
    assert places.with_level("projects/app", "read") == {"projects": "read"}
    assert places.with_level("Documents", "hidden") == {"projects": "read", "projects/app": "write"}


def test_browsing_lists_names_with_their_level_never_links_hidden_or_credential_names(home: Path) -> None:
    places = PlaceMap(working_directory=home, levels={"projects/app": "read"})
    entries, cursor = places.browse("projects", 0)
    assert [(entry.name, entry.kind, entry.level) for entry in entries] == [("app", "folder", "read"), ("secret", "folder", "hidden")]
    assert cursor is None
    with pytest.raises(PermissionError, match="hidden"):
        places.browse(".ssh", 0)


def test_browsing_pages_through_a_large_folder(home: Path) -> None:
    for index in range(PlaceMap.PAGE + 5):
        (home / "Documents" / f"note-{index:04}.txt").write_text("x")
    places = PlaceMap(working_directory=home)
    first, cursor = places.browse("Documents", 0)
    rest, end = places.browse("Documents", cursor)
    assert len(first) == PlaceMap.PAGE and len(rest) == 5 and end is None


def test_the_browse_budget_refuses_past_its_window() -> None:
    budget = BrowseBudget(pages=2, window=60)
    budget.take()
    budget.take()
    with pytest.raises(PermissionError, match="limited to 2 pages"):
        budget.take()


def test_browsing_the_home_folder_leaves_out_this_systems_credential_stores(home: Path) -> None:
    """This system's store of browser and password-manager profiles (`snap` on Linux, `AppData` on
    Windows, `Library/Keychains` on macOS) is not listed below home."""
    store = next(paths for key, paths in NEVER_GRANTABLE.home.items() if sys.platform.startswith(key))[0]
    parent, _, name = store.rpartition("/")
    (home / store).mkdir(parents=True)
    places = PlaceMap(working_directory=home)
    assert name not in {entry.name.casefold() for entry in places.browse(parent, 0)[0]}
    assert "Documents" in {entry.name for entry in places.browse("", 0)[0]}


@pytest.mark.parametrize("name", ["server.key", "release.jks", "app.keystore", "terraform.tfstate", "kubeconfig", "service-account-prod.json"])
def test_more_credential_files_are_never_grantable(name: str) -> None:
    assert NEVER_GRANTABLE.refusal((name,), None) is not None
