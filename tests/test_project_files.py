"""The web sees a project folder's files read-only: nothing hidden, secret or linked is listed or read, text leaves redacted."""

import asyncio
import base64
import gzip
import os
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from galaius_core import MACHINE_AGENT_REQUESTS, PROJECT_TEXT_MAX, MachineAgentAnswer
from galaius.machine_agents import MachineAgents, WebRuns
from galaius.pinned_directory import PinnedDirectory
from galaius.project_files import ProjectFolder
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


def _agents(project: Path, tmp_path: Path, traced: list | None = None) -> MachineAgents:
    return MachineAgents(roots={"dev": project.parent}, permission="read_only", run_agents=True, session="web-test", runs=WebRuns(path=tmp_path / "web-runs.json"),
                         environment={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "SERVICE_TOKEN": KNOWN}, cli=("false",),
                         places=PlaceMap(working_directory=tmp_path), audit=lambda log, entry: (traced if traced is not None else []).append((log, entry)))


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
                                  "README.md.", "README.md ", "README.md:stream", "src\x01x", ".vscode/settings.json", ".GITHUB/ci.yml",
                                  "id_rsa", "src/cert.pem", "keys/ID_ED25519", "ops/client_secret_web.json"])
def test_hidden_relative_and_absolute_paths_are_refused_before_the_disk(path: str) -> None:
    with pytest.raises(ValueError):
        MACHINE_AGENT_REQUESTS.validate_python({"id": str(uuid4()), "machine": {"id": str(uuid4())}, "workspace_id": str(uuid4()), "initiator_account": str(uuid4()),
                                                "expires_at": datetime.now(UTC).isoformat(), "signature": "0" * 64, "op": "file", "root": "dev", "base": "app", "path": path})


@pytest.mark.parametrize("path", ["out/anything", "src/linked.py", "out"])
def test_links_are_never_read(project: Path, tmp_path: Path, path: str) -> None:
    with pytest.raises(PermissionError):
        _ask(_agents(project, tmp_path), "file", path=path)


@pytest.mark.parametrize(("path", "kind", "check"), [
    ("src/deep/main.py", "text", lambda content: content.text == "print('changed')\n" and content.git == "modified"),
    ("notes.txt", "text", lambda content: SHAPED not in content.text and KNOWN not in content.text and content.git == "untracked"),
    ("logo.png", "image", lambda content: content.content_type == "image/png" and base64.b64decode(content.data) == PNG),
    ("blob.bin", "binary", lambda content: content.text is None and content.size == 200),
    ("big.log", "text", lambda content: content.truncated and len(content.text) == content.end == PROJECT_TEXT_MAX),
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
    # A log full of the vault value shrinks once masked; the split copy at the cut must not show.
    value = "Zq9x" + "vault" + "Secret77"
    (project / ".env").write_text(f'DB_PASSWORD="{value}" # the vault\n')
    unit = f"{value}\n" if lines else f"{value} "
    body = unit * (PROJECT_TEXT_MAX // len(unit))
    (project / "full.log").write_text(body + value + "tail" * 100)
    text = _ask(_agents(project, tmp_path), "file", path="full.log").content.text
    assert value[:6] not in text and "•••" in text


def test_a_linked_env_file_still_masks_its_values(project: Path, tmp_path: Path) -> None:
    # A `.env` that is a link is never shown, but its values are masked everywhere.
    (tmp_path / "vault.env").write_text("pw=" + "Zq9x" + "LinkedValue\n")
    (project / ".env").unlink()
    (project / ".env").symlink_to(tmp_path / "vault.env")
    (project / "uses.txt").write_text("connect with Zq9xLinkedValue now\n")
    text = _ask(_agents(project, tmp_path), "file", path="uses.txt").content.text
    assert "Zq9xLinkedValue" not in text


def test_every_read_leaves_its_trace_in_the_pcs_places_log_refused_ones_too(project: Path, tmp_path: Path) -> None:
    # A probe the PC refuses is traced as a read it answers.
    traced: list = []
    agents = _agents(project, tmp_path, traced)
    _ask(agents, "file", path="README.md")
    with pytest.raises(PermissionError):
        _ask(agents, "file", path="src/linked.py")
    _ask(agents, "find", query="main")
    assert [(log, entry["read"], entry.get("path"), entry.get("query"), entry["refused"] is not None) for log, entry in traced] == [
        ("places.log", "file", "README.md", None, False), ("places.log", "file", "src/linked.py", None, True), ("places.log", "find", None, "main", False)]


def test_a_pc_without_its_levels_shows_no_project_files(project: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        _ask(_agents(project, tmp_path).model_copy(update={"places": None}), "files")


def test_a_folder_swapped_for_a_link_after_its_listing_is_never_entered(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each folder is entered from its parent's pinned handle, so a swap between the name
    # check and the open meets the link itself and is refused.
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "stolen.txt").write_text("x\n")
    real = PinnedDirectory.selected.get().names

    def swapping(directory) -> list[str]:
        names = real(directory)
        if directory.path == project / "src" and (project / "src" / "deep").is_dir() and not (project / "src" / "deep").is_symlink():
            (project / "src" / "deep").rename(project / "src" / "moved")
            (project / "src" / "deep").symlink_to(tmp_path / "elsewhere")
        return names

    monkeypatch.setattr(PinnedDirectory.selected.get(), "names", swapping)
    with pytest.raises(PermissionError):
        _ask(_agents(project, tmp_path), "files", path="src/deep")


def test_a_change_inside_a_hidden_child_folder_marks_nothing(project: Path, tmp_path: Path) -> None:
    (project / "docs" / ".cache").mkdir(parents=True)
    (project / "docs" / ".cache" / "state").write_text("x\n")
    subprocess.run(("git", "add", "-f", "docs/.cache/state"), cwd=project, check=True)
    assert "docs" not in {entry.name: entry.git for entry in _ask(_agents(project, tmp_path), "files").files if entry.git}


@pytest.mark.parametrize(("line", "value"), [("DB_PASS=abc#defghijk", "abc#defghijk"), ("K=Xy7#Qp2LmN9zRt # note", "Xy7#Qp2LmN9zRt"),
                                             ('export A="ab\\"cdefghij" # c', 'ab"cdefghij'), ('export A="ab\\"cdefghij" # c', 'ab\\"cdefghij'),
                                             ("B='q uoted value' # c", "q uoted value")])
def test_an_env_value_is_masked_whole_whatever_it_holds(project: Path, tmp_path: Path, line: str, value: str) -> None:
    # A `#` inside an unquoted value, or an escaped quote (as written or as read), must not shorten what is masked.
    (project / ".env").write_text(line + "\n")
    (project / "uses.txt").write_text(f"connect with {value} now\n")
    assert value not in _ask(_agents(project, tmp_path), "file", path="uses.txt").content.text


PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"   # no NUL in it


@pytest.mark.parametrize(("path", "offset", "length", "expected"), [("blob.bin", 0, 200, b"\x00\x01" * 100), ("blob.bin", 7, 10, (b"\x00\x01" * 100)[7:17]),
                                                                   ("blob.bin", 190, 50, (b"\x00\x01" * 100)[190:]), ("blob.bin", 200, 5, b""), ("blob.bin", 10**9, 5, b""),
                                                                   ("logo.png", 0, 8, PNG[:8]), ("plan.pdf", 9, 40, PDF[9:49])])
def test_a_bytes_read_answers_exactly_the_part_asked_and_its_size(project: Path, tmp_path: Path, path: str, offset: int, length: int, expected: bytes) -> None:
    """A PDF holding no NUL is a document, read raw, never shown as its source text."""
    (project / "plan.pdf").write_bytes(PDF)
    assert _ask(_agents(project, tmp_path), "file", path="plan.pdf").content.kind == "binary"
    part = _ask(_agents(project, tmp_path), "bytes", path=path, offset=offset, length=length).part
    assert (part.content, part.offset, part.size) == (expected, offset, (project / path).stat().st_size)


@pytest.mark.parametrize("path", ["notes.txt", "README.md", "src/linked.py", "out/anything", "media.bin", "missing.bin"])
def test_a_bytes_read_never_leaves_text_unredacted_nor_follows_a_link(project: Path, tmp_path: Path, path: str) -> None:
    (project / "media.bin").symlink_to(project / "blob.bin")
    with pytest.raises(PermissionError):
        _ask(_agents(project, tmp_path), "bytes", path=path, length=10)


def test_a_bytes_read_of_an_env_file_is_refused_before_the_disk(project: Path, tmp_path: Path) -> None:
    (project / ".env.media").write_bytes(b"\x00" + KNOWN.encode())
    with pytest.raises(ValueError):
        _ask(_agents(project, tmp_path), "bytes", path=".env.media", length=10)


def _pages(agents, path: str, offset: int = 0) -> list:
    pages = [_ask(agents, "file", path=path, offset=offset).content]
    while pages[-1].truncated:
        pages.append(_ask(agents, "file", path=path, offset=pages[-1].end).content)
    return pages


@pytest.mark.parametrize(("encoding", "mark"), [("utf-8", 0), ("utf-16", 2)])
def test_pages_asked_each_from_the_end_of_the_last_join_into_the_whole_file_and_a_tail_starts_where_asked(project: Path, tmp_path: Path, encoding: str,
                                                                                                         mark: int) -> None:
    """A log past one page is read to its tail, page after page, nothing lost or repeated between them; UTF-16 as UTF-8."""
    body = "".join(f"2026-10-28 15:{index % 60:02d} worker {index} took {index * 7 % 997} ms é\n" for index in range(70_000))
    data = body.encode(encoding)
    (project / "big.log").write_bytes(data)
    agents = _agents(project, tmp_path)
    pages = _pages(agents, "big.log")
    assert "".join(page.text for page in pages) == body and all(page.text.endswith("\n") for page in pages)
    assert [page.offset for page in pages] == [mark, *(page.end for page in pages[:-1])] and pages[-1].end == len(data)
    tail = _ask(agents, "file", path="big.log", offset=len(data) - 200).content
    assert (tail.offset, tail.end, tail.truncated) == (len(data) - 200, len(data), False) and body.endswith(tail.text)


@pytest.mark.parametrize("lines", [True, False], ids=["lines", "one-line"])
def test_a_secret_split_by_a_page_start_never_leaves(project: Path, tmp_path: Path, lines: bool) -> None:
    value = "Zq9x" + "vault" + "Secret77"
    (project / ".env").write_text(f'DB_PASSWORD="{value}" # the vault\n')
    unit = f"{value}\n" if lines else f"{value} "
    (project / "full.log").write_text(unit * 5000)
    agents = _agents(project, tmp_path)
    for offset in range(len(unit) * 100, len(unit) * 101 + 1):
        content = _ask(agents, "file", path="full.log", offset=offset).content
        assert value[-6:] not in content.text and "Secret" not in content.text


# A private key's frame, built in pieces so no secret scanner mistakes the fixture for a real key.
PEM_BEGIN, PEM_END = "-----BEGIN " + "RSA PRIVATE KEY-----", "-----END " + "RSA PRIVATE KEY-----"
PEM_BODY = tuple(f"MIIEow{index:02d}" + "QkFTRTY0Ym9keQ" * 4 for index in range(20))


def _texts(agents, path: str, offsets) -> str:
    return "\n".join(_ask(agents, "file", path=path, offset=offset).content.text for offset in offsets)


@pytest.mark.parametrize("before", [10, PROJECT_TEXT_MAX - 300], ids=["key-near-the-start", "key-across-the-page-cut"])
def test_a_private_key_never_shows_whatever_page_holds_part_of_it(project: Path, tmp_path: Path, before: int) -> None:
    """Pages asked from any line of the key (the one after its BEGIN line included), or chained across the cut, mask it."""
    filler = "".join(f"line {index:07d}\n" for index in range(before // 13 + 1))[:before]
    key = "\n".join((PEM_BEGIN, *PEM_BODY, PEM_END)) + "\n"
    (project / "deploy.txt").write_text(filler + key + "after\n" * 1000)
    agents = _agents(project, tmp_path)
    at = len(filler)
    lines = [at + key.index(line) for line in (*PEM_BODY, PEM_END)]
    shown = _texts(agents, "deploy.txt", (*lines, *(line + 7 for line in lines))) + "".join(page.text for page in _pages(agents, "deploy.txt"))
    assert not any(line[6:] in shown for line in PEM_BODY) and "•••" in shown


def test_a_pc_secret_holding_a_line_end_never_shows_at_either_cut(project: Path, tmp_path: Path) -> None:
    secret = "first-half-" + "Zq9xA1\n" + "second-half-" + "Zq9xB2"
    agents = _agents(project, tmp_path).model_copy(update={"environment": {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "SERVICE_TOKEN": secret}})
    # The page's last line end falls between the secret's two lines, and the next page starts there.
    head = "x" * 50 + "\n"
    filler = head * ((PROJECT_TEXT_MAX - 2000) // len(head))
    body = filler + "y" * (PROJECT_TEXT_MAX - len(filler) - len(secret) + 10) + secret + " tail\n" * 10
    (project / "env.log").write_text(body)
    pages = _pages(agents, "env.log")
    assert body.index(secret) < pages[0].end < body.index(secret) + len(secret)   # the cut splits it
    starts = range(body.index(secret), body.index(secret) + len(secret) + 1)
    shown = "".join(page.text for page in pages) + _texts(agents, "env.log", starts)
    assert "Zq9xA1" not in shown and "Zq9xB2" not in shown


def test_a_utf16_text_file_is_shown_decoded_and_redacted_never_raw(project: Path, tmp_path: Path) -> None:
    (project / "setup.ps1").write_bytes("﻿$env:SERVICE = '{}'\r\nWrite-Host ok\r\n".format(KNOWN).encode("utf-16-le"))
    content = _ask(_agents(project, tmp_path), "file", path="setup.ps1").content
    assert content.kind == "text" and "Write-Host ok" in content.text and KNOWN not in content.text and content.end == content.size
    with pytest.raises(PermissionError, match="shown as text"):
        _ask(_agents(project, tmp_path), "bytes", path="setup.ps1", length=10)


def _zip(path: Path, files: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path.read_bytes()


@pytest.mark.parametrize(("name", "make"), [
    ("backup.zip", lambda folder: _zip(folder / "backup.zip", {"app/.env": f"SERVICE_TOKEN={KNOWN}\n".encode(), "app/main.py": b"print(1)\n" * 50})),
    ("state.db", lambda folder: (folder / "state.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 3000 + KNOWN.encode("utf-16-le") + b"\x00" * 100)),
    ("key.bin", lambda folder: (folder / "key.bin").write_bytes(b"\x00\x01" * 100 + (PEM_BEGIN + "\n" + PEM_BODY[0]).encode() + b"\x00" * 100)),
    ("logs.tar.gz", lambda folder: (folder / "logs.tar.gz").write_bytes(gzip.compress(b"\x00" * 512 + f"token: {SHAPED}\n".encode()))),
])
def test_raw_bytes_holding_a_secret_never_leave(project: Path, tmp_path: Path, name: str, make) -> None:
    """A packed file is unpacked to be checked; any other but a proven media kind is searched in UTF-8 and UTF-16."""
    make(project)
    with pytest.raises(PermissionError, match="holds a secret"):
        _ask(_agents(project, tmp_path), "bytes", path=name, length=10)


def test_a_clean_zip_downloads_and_a_large_file_is_checked_part_by_part(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _zip(project / "dist.zip", {"app/main.py": b"print(1)\n" * 50, "app/logo.png": PNG})
    part = _ask(_agents(project, tmp_path), "bytes", path="dist.zip", length=len(data)).part
    assert part.content == data
    monkeypatch.setattr(ProjectFolder, "WHOLE", 0)
    at = 3 * 1024 * 1024
    (project / "weights.bin").write_bytes(b"\x00" * at + KNOWN.encode() + b"\x00" * at)
    agents = _agents(project, tmp_path)
    assert _ask(agents, "bytes", path="weights.bin", offset=0, length=1024 * 1024).part.content == b"\x00" * 1024 * 1024
    for offset in (at - 1024 * 1024 - 100, at):   # the part ending just short of the secret (its overlap reaches it), the one holding it
        with pytest.raises(PermissionError, match="holds a secret"):
            _ask(agents, "bytes", path="weights.bin", offset=offset, length=1024 * 1024)


def test_a_players_later_parts_leave_no_trace_its_first_and_refused_ones_do(project: Path, tmp_path: Path) -> None:
    traced: list = []
    agents = _agents(project, tmp_path, traced)
    for offset in (0, 50, 100):
        _ask(agents, "bytes", path="blob.bin", offset=offset, length=50)
    with pytest.raises(PermissionError):
        _ask(agents, "bytes", path="notes.txt", offset=50, length=50)
    assert [(entry["path"], entry.get("offset", 0), entry["refused"] is not None) for _, entry in traced] == [("blob.bin", 0, False), ("notes.txt", 50, True)]
