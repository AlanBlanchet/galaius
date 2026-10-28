"""The web sees a project folder's files read-only: nothing hidden, secret or linked is listed or read, text leaves redacted."""

import asyncio
import base64
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from galaius_core import MACHINE_AGENT_REQUESTS, PROJECT_TEXT_MAX, MachineAgentAnswer
from galaius.machine_agents import MachineAgents, WebRuns
from galaius.places import PlaceMap

# Built in pieces so no secret scanner mistakes the fixture for a real key.
SHAPED = "sk-" + "abcdefghijklmnop"
KNOWN = "topsecret" + "value123"
NOTES = "api" + "_key" + " = " + SHAPED + "\nsecret value " + KNOWN + "\n"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """`dev/app`: a git repository with one committed file then modified, secrets, a link out and a large text."""
    folder = tmp_path / "dev" / "app"
    (folder / "src" / "deep").mkdir(parents=True)
    (folder / ".github").mkdir()
    files = {"src/deep/main.py": "print('hi')\n", "README.md": "# app\n", ".env.local": "TOKEN=x\n", ".env": "A=1\n", "id_rsa": "key\n", "src/cert.pem": "pem\n",
             ".github/ci.yml": "on: push\n", "notes.txt": NOTES, "big.log": "x" * (PROJECT_TEXT_MAX + 10)}
    for name, text in files.items():
        (folder / name).write_text(text)
    (folder / "logo.png").write_bytes(PNG)
    (folder / "blob.bin").write_bytes(b"\x00\x01" * 100)
    (folder / "out").symlink_to(tmp_path)
    (folder / "src" / "linked.py").symlink_to(folder / "README.md")
    git = ("git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false")
    subprocess.run((*git, "init", "-q"), cwd=folder, check=True)
    subprocess.run((*git, "add", "src/deep/main.py", "README.md"), cwd=folder, check=True)
    subprocess.run((*git, "commit", "-qm", "one"), cwd=folder, check=True)
    (folder / "src" / "deep" / "main.py").write_text("print('changed')\n")
    return folder


def _agents(project: Path, tmp_path: Path) -> MachineAgents:
    return MachineAgents(roots={"dev": project.parent}, permission="read_only", run_agents=True, session="web-test", runs=WebRuns(path=tmp_path / "web-runs.json"),
                         environment={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "SERVICE_TOKEN": KNOWN}, cli=("false",))


def _ask(agents: MachineAgents, op: str, **fields) -> MachineAgentAnswer:
    request = MACHINE_AGENT_REQUESTS.validate_python({"id": str(uuid4()), "machine": {"id": str(uuid4())}, "workspace_id": str(uuid4()), "initiator_account": str(uuid4()),
                                                      "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(), "signature": "0" * 64, "op": op,
                                                      "root": "dev", "base": "app", **fields})
    return asyncio.run(agents.answer(request))


def test_a_listing_shows_plain_entries_with_git_marks_and_counts_what_it_hid(project: Path, tmp_path: Path) -> None:
    agents = _agents(project, tmp_path)
    root = _ask(agents, "files")
    assert [(entry.name, entry.kind, entry.git) for entry in root.files] == [
        (".github", "folder", "untracked"), ("src", "folder", "modified"), ("big.log", "file", "untracked"), ("blob.bin", "file", "untracked"),
        ("logo.png", "file", "untracked"), ("notes.txt", "file", "untracked"), ("README.md", "file", None)]
    assert root.hidden == 5   # .git, .env, .env.local, id_rsa, the link `out`
    deep = _ask(agents, "files", path="src")
    assert [(entry.name, entry.git) for entry in deep.files] == [("deep", "modified")] and deep.hidden == 2   # cert.pem, the link linked.py
    assert [(entry.name, entry.git) for entry in _ask(agents, "files", path="src/deep").files] == [("main.py", "modified")]


@pytest.mark.parametrize("path", ["..", ".git/config", ".env.local", "src/../id_rsa", "/etc/passwd", "a\\b",
                                  "README.md.", "README.md ", "README.md:stream", "src\x01x", ".vscode/settings.json", ".GITHUB/ci.yml"])
def test_hidden_relative_and_absolute_paths_are_refused_before_the_disk(path: str) -> None:
    with pytest.raises(ValueError):
        MACHINE_AGENT_REQUESTS.validate_python({"id": str(uuid4()), "machine": {"id": str(uuid4())}, "workspace_id": str(uuid4()), "initiator_account": str(uuid4()),
                                                "expires_at": datetime.now(UTC).isoformat(), "signature": "0" * 64, "op": "file", "root": "dev", "base": "app", "path": path})


@pytest.mark.parametrize("path", ["id_rsa", "src/cert.pem", "out/anything", "src/linked.py", "out"])
def test_credential_stores_and_links_are_never_read(project: Path, tmp_path: Path, path: str) -> None:
    with pytest.raises(PermissionError):
        _ask(_agents(project, tmp_path), "file", path=path)


@pytest.mark.parametrize(("path", "kind", "check"), [
    ("src/deep/main.py", "text", lambda content: content.text == "print('changed')\n" and content.git == "modified"),
    ("notes.txt", "text", lambda content: SHAPED not in content.text and KNOWN not in content.text and content.git == "untracked"),
    ("logo.png", "image", lambda content: content.content_type == "image/png" and base64.b64decode(content.data) == PNG),
    ("blob.bin", "binary", lambda content: content.text is None and content.size == 200),
    ("big.log", "text", lambda content: content.truncated and PROJECT_TEXT_MAX - 1024 <= len(content.text) < PROJECT_TEXT_MAX),
])
def test_a_file_comes_as_text_image_or_facts(project: Path, tmp_path: Path, path: str, kind: str, check) -> None:
    content = _ask(_agents(project, tmp_path), "file", path=path).content
    assert content.kind == kind and check(content)


def test_find_reads_the_repository_and_never_returns_a_hidden_secret_or_linked_file(project: Path, tmp_path: Path) -> None:
    agents = _agents(project, tmp_path)
    (project / ".gitignore").write_text("ignored.py\n")
    (project / "ignored.py").write_text("")
    assert _ask(agents, "find", query="MAIN").found == ("src/deep/main.py",)
    found = set(_ask(agents, "find", query=".").found)
    assert {"README.md", "src/deep/main.py", ".github/ci.yml", "blob.bin"} <= found
    assert not found & {".env", ".env.local", "src/cert.pem", "src/linked.py", "ignored.py"}


def test_a_case_alias_never_opens_the_entry_it_stands_for(project: Path, tmp_path: Path) -> None:
    """On a case-insensitive disk `readme.md` would open `README.md`; only the name its folder lists opens."""
    with pytest.raises(PermissionError):
        _ask(_agents(project, tmp_path), "file", path="readme.md")


def test_only_listed_dot_names_show(project: Path, tmp_path: Path) -> None:
    for name in (".vscode", ".idea"):
        (project / name).mkdir()
    (project / ".gitignore").write_text("x\n")
    names = [entry.name for entry in _ask(_agents(project, tmp_path), "files").files]
    assert ".github" in names and ".gitignore" in names and not {".vscode", ".idea", ".git", ".env"} & set(names)


@pytest.mark.parametrize("config", ["filter.lfs.clean=cat", "core.fsmonitor=true", "include.path=/elsewhere"])
def test_a_repository_whose_config_runs_code_shows_no_marks(project: Path, tmp_path: Path, config: str) -> None:
    key, value = config.split("=", 1)
    subprocess.run(("git", "config", "--local", key, value), cwd=project, check=True)
    assert all(entry.git is None for entry in _ask(_agents(project, tmp_path), "files").files)


def test_a_folder_put_here_from_the_web_shows_no_marks(project: Path, tmp_path: Path) -> None:
    agents = _agents(project, tmp_path).model_copy(update={"workspaces": SimpleNamespace(placed=lambda: [(None, project)])})
    assert all(entry.git is None for entry in _ask(agents, "files").files)


def test_values_of_the_projects_env_files_are_masked_even_across_the_cut(project: Path, tmp_path: Path) -> None:
    (project / ".env").write_text("DATABASE_URL='postgres://vault-value-1234'\n")
    (project / "config.txt").write_text("url postgres://vault-value-1234 here\n")
    (project / "edge.txt").write_text("x" * (PROJECT_TEXT_MAX - 5) + "postgres://vault-value-1234")
    agents = _agents(project, tmp_path)
    assert "vault-value-1234" not in _ask(agents, "file", path="config.txt").content.text
    edge = _ask(agents, "file", path="edge.txt").content
    assert edge.truncated and "postg" not in edge.text[-10:]


def test_a_folder_its_owner_set_hidden_is_left_out_and_never_opened(project: Path, tmp_path: Path) -> None:
    agents = _agents(project, tmp_path).model_copy(update={"places": PlaceMap(working_directory=tmp_path, levels={"dev/app/src": "hidden"})})
    root = _ask(agents, "files")
    assert "src" not in [entry.name for entry in root.files] and root.hidden == 6
    with pytest.raises(PermissionError):
        _ask(agents, "file", path="src/deep/main.py")
    assert "src/deep/main.py" not in _ask(agents, "find", query="main").found


@pytest.mark.parametrize("lines", [True, False], ids=["lines", "one-line"])
def test_a_secret_split_by_the_cut_never_leaves_even_when_masks_shrink_the_text(project: Path, tmp_path: Path, lines: bool) -> None:
    # code-reviewer 0284cc66 row 1: a log full of the vault value shrinks once masked; the split copy at the cut must not show.
    value = "Zq9x" + "vault" + "Secret77"
    (project / ".env").write_text(f'DB_PASSWORD="{value}" # the vault\n')
    unit = f"{value}\n" if lines else f"{value} "
    body = unit * (PROJECT_TEXT_MAX // len(unit))
    (project / "full.log").write_text(body + value + "tail" * 100)
    text = _ask(_agents(project, tmp_path), "file", path="full.log").content.text
    assert value[:6] not in text and "•••" in text


def test_a_linked_env_file_still_masks_its_values(project: Path, tmp_path: Path) -> None:
    # code-reviewer 0284cc66 row 2: a `.env` that is a link is never shown, but its values are masked everywhere.
    (tmp_path / "vault.env").write_text("pw=" + "Zq9x" + "LinkedValue\n")
    (project / ".env").unlink()
    (project / ".env").symlink_to(tmp_path / "vault.env")
    (project / "uses.txt").write_text("connect with Zq9xLinkedValue now\n")
    text = _ask(_agents(project, tmp_path), "file", path="uses.txt").content.text
    assert "Zq9xLinkedValue" not in text
