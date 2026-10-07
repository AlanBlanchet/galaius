"""Folder names a PC derives from server text (a workspace name) stay one safe folder each,
and two workspaces sharing a name never share a folder (`galaius.prompt_mirror`)."""

import json
from uuid import uuid4

import pytest

from galaius.prompt_mirror import PromptWorkspace, folder_name, folders, follow_renames, freeze_legacy


@pytest.mark.parametrize(("text", "name"), [
    ("Acme Conseil SAS", "acme-conseil-sas"),
    ("someone@example.com", "someone@example.com"),
    ("Société Générale", "societe-generale"),
    ("../../etc", "etc"),
    ("a/b\\c\x00d", "a-b-c-d"),
    ("..", "workspace"),
    ("x" * 200, "x" * 80),
])
def test_server_text_becomes_one_safe_folder(text, name):
    assert folder_name(text) == name


def test_each_workspace_gets_one_folder_and_shared_or_legacy_names_get_the_id():
    def workspace(label, kind="company", link=False):
        return PromptWorkspace(workspace_id=uuid4(), name=label, kind=kind, label=label, link=link, can_write=link)
    own, first, second, other, legacy = workspace("Alan Blanchet EI", link=True), workspace("Acme"), workspace("ACME"), workspace("Other"), workspace("Personal")
    places = folders((own, first, second, other, legacy))
    assert places[own.workspace_id] == ("alan-blanchet-ei",)
    assert places[other.workspace_id] == ("other",)
    assert places[first.workspace_id] == (f"acme-{str(first.workspace_id)[:8]}",)
    assert len({places[first.workspace_id], places[second.workspace_id]}) == 2
    assert places[legacy.workspace_id] == (f"personal-{str(legacy.workspace_id)[:8]}",)


def test_earlier_layout_is_kept_read_only_never_removed(tmp_path):
    prompt = tmp_path / "personal" / "someone@example.com" / "paradigms" / "coding.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("text")
    assert freeze_legacy(tmp_path) == (tmp_path / "personal",)
    assert prompt.read_text() == "text"
    assert prompt.stat().st_mode & 0o222 == 0 and prompt.parent.stat().st_mode & 0o222 == 0
    assert freeze_legacy(tmp_path) == ()  # once
    for path in (prompt.parent, prompt.parent.parent, tmp_path / "personal"):
        path.chmod(0o700)  # let tmp_path clean up


def _copy(root, folder, workspace, files):
    for relative, text in files.items():
        (root / folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / folder / relative).write_text(text)
    (root / folder / ".mirror.json").write_text(json.dumps({"workspace_id": str(workspace), "files": {key: "x" for key in files}}))


def test_a_renamed_workspace_moves_its_folder_or_drops_its_old_copy(tmp_path):
    renamed, merged = uuid4(), uuid4()
    _copy(tmp_path, "my-workspace", renamed, {"paradigms/coding.md": "a"})
    _copy(tmp_path, "old-name", merged, {"paradigms/coding.md": "a"})
    (tmp_path / "old-name" / "paradigms" / "mine.txt").write_text("written by hand")
    _copy(tmp_path, "new-name", merged, {"paradigms/coding.md": "a"})
    notes = follow_renames(tmp_path, {renamed: ("alan-blanchet-ei",), merged: ("new-name",)})
    assert len(notes) == 2 and not (tmp_path / "my-workspace").exists()
    assert (tmp_path / "alan-blanchet-ei" / "paradigms" / "coding.md").read_text() == "a"
    assert sorted(path.name for path in (tmp_path / "old-name").rglob("*")) == ["mine.txt", "paradigms"]  # only what the mirror wrote went
    assert follow_renames(tmp_path, {renamed: ("alan-blanchet-ei",), merged: ("new-name",)}) == ()
