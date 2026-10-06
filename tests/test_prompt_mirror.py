"""Folder names a PC derives from server text (an email, a company name) stay one safe folder each,
and two workspaces sharing a name never share a folder (`interact.prompt_mirror`)."""

from uuid import uuid4

import pytest

from interact.prompt_mirror import PromptWorkspace, folder_name, folders


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


def test_two_companies_sharing_a_name_get_their_own_folders():
    def company(label):
        return PromptWorkspace(workspace_id=uuid4(), name=label, kind="company", label=label, link=False, can_write=False)
    personal = PromptWorkspace(workspace_id=uuid4(), name="Personal", kind="personal", label="acme", link=True, can_write=False)
    first, second, other = company("Acme"), company("ACME"), company("Other")
    places = folders((personal, first, second, other))
    assert places[personal.workspace_id] == ("personal", "acme")
    assert places[other.workspace_id] == ("company", "other")
    assert places[first.workspace_id] == ("company", f"acme-{str(first.workspace_id)[:8]}")
    assert len({places[first.workspace_id], places[second.workspace_id]}) == 2
