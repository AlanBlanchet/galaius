"""Tests for scripts/sync_prompts.py — the per-developer prompts overlay materializer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(module: str):
    spec = importlib.util.spec_from_file_location(module, _SCRIPTS / f"{module}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sync_mod(tmp_path, monkeypatch):
    """Load sync_prompts with its REPO_ROOT pointed at a scratch tree."""
    mod = _load("sync_prompts")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(mod, "_MANIFEST", tmp_path / "repo" / ".github" / "agents" / ".sync-prompts.json")
    return mod


def _write(d: Path, *names: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text(f"# {n}\n")


def test_flat_prompts_dir_routes_each_layer_to_its_overlay(sync_mod, tmp_path):
    src = tmp_path / "user-prompts"
    _write(src, "librarian.agent.md", "alan.instructions.md", "x.prompt.md", "release.skill.md", "note.md")
    rc = sync_mod.sync(src, into=None, ext=".md", mode="copy", dry=False)
    assert rc == 0
    root = sync_mod.REPO_ROOT
    assert (root / ".github/agents/librarian.agent.md").is_file()
    assert (root / ".github/instructions/alan.instructions.md").is_file()
    assert (root / ".github/prompts/x.prompt.md").is_file()
    assert (root / ".github/skills/release/SKILL.md").is_file()
    assert not (root / ".github/agents/note.md").exists()  # no recognizable layer → skipped


def test_uniform_source_materializes_every_file_to_one_overlay(sync_mod, tmp_path):
    src = tmp_path / "agents"
    _write(src, "advocate.md", "tester.md")
    rc = sync_mod.sync(src, into="agents", ext=".md", mode="copy", dry=False)
    assert rc == 0
    assert (sync_mod.REPO_ROOT / ".github/agents/advocate.agent.md").is_file()
    assert (sync_mod.REPO_ROOT / ".github/agents/tester.agent.md").is_file()


def test_skill_source_becomes_skill_md_under_named_folder(sync_mod, tmp_path):
    src = tmp_path / "skills" / "coding"
    _write(src, "SKILL.md")
    rc = sync_mod.sync(src, into="skills", ext=".md", mode="copy", dry=False)
    assert rc == 0
    assert (sync_mod.REPO_ROOT / ".github/skills/SKILL/SKILL.md").is_file()


def test_dry_run_writes_nothing(sync_mod, tmp_path):
    src = tmp_path / "p"
    _write(src, "librarian.agent.md")
    rc = sync_mod.sync(src, into=None, ext=".md", mode="copy", dry=True)
    assert rc == 0
    assert not (sync_mod.REPO_ROOT / ".github").exists()


def test_sync_writes_a_manifest_for_re_materialization(sync_mod, tmp_path):
    src = tmp_path / "p"
    _write(src, "librarian.agent.md")
    sync_mod.sync(src, into=None, ext=".md", mode="copy", dry=False)
    manifest = json.loads(sync_mod._MANIFEST.read_text())
    assert manifest["source"] == str(src)
    assert manifest["files"] == [".github/agents/librarian.agent.md"]


def test_resync_prunes_entries_with_no_source_counterpart(sync_mod, tmp_path):
    src = tmp_path / "p"
    _write(src, "a.agent.md", "b.agent.md")
    sync_mod.sync(src, into=None, ext=".md", mode="copy", dry=False)
    (src / "b.agent.md").unlink()  # upstream deleted it
    sync_mod.sync(src, into=None, ext=".md", mode="copy", dry=False)
    agents = sync_mod.REPO_ROOT / ".github/agents"
    assert (agents / "a.agent.md").exists()
    assert not (agents / "b.agent.md").exists()


def test_missing_source_fails_loudly(sync_mod, tmp_path, capsys):
    rc = sync_mod.sync(tmp_path / "nope", into=None, ext=".md", mode="copy", dry=False)
    assert rc == 1
    assert "not a directory" in capsys.readouterr().err


# ── The faults found reviewing the first cut ────────────────────────────────────────────────────


def test_the_default_source_is_found_on_every_platform(monkeypatch):
    """A hard-coded ~/.config/Code/User/prompts is a Linux path.

    interact ships on macOS and Windows (CI drives real GUI sessions on both), and a teammate there
    running `python scripts/sync_prompts.py` with no arguments would be told their own prompts store
    does not exist. The store's location is per-platform, and the project's own `vscode-sync.sh`
    already knew that — this just did not follow it.
    """
    sp = _load("sync_prompts")

    mac = sp.default_source("Darwin", Path("/Users/x"))
    assert mac == Path("/Users/x/Library/Application Support/Code/User/prompts"), mac
    win = sp.default_source("Windows", Path("C:/Users/x"))
    assert "Code" in str(win) and "prompts" in str(win), win
    lin = sp.default_source("Linux", Path("/srv/x"))
    assert lin == Path("/srv/x/.config/Code/User/prompts"), lin


def test_a_file_the_global_store_already_provides_is_not_duplicated(tmp_path):
    """The fault Alan hit: "In copilot, i also have double the agents for each files."

    VS Code discovers customizations from the user's OWN prompts store AND from the workspace's
    `.github`. Mirroring a store that already serves every workspace means the editor finds the same
    agent twice and lists it twice. The overlay exists so a developer WITHOUT that store gets one —
    not to hand a second copy to the developer who already has it.
    """
    sp = _load("sync_prompts")

    store = tmp_path / "store"
    store.mkdir()
    (store / "tester.md").write_text("shared")
    (store / "only-here.md").write_text("local")

    # The same store is already serving the editor globally.
    report = sp.sync_report(store, into="agents", ext=".md", mode="copy", dry=False,
                     repo=tmp_path / "repo", already=[store])
    assert report["skipped_duplicate"] == 2, report
    assert not (tmp_path / "repo" / ".github" / "agents" / "tester.agent.md").exists()


def test_a_file_the_global_store_does_NOT_provide_is_still_materialized(tmp_path):
    """The other half: the mechanism must still work. A teammate whose store lacks an agent — or who
    has no store at all — gets it in the overlay."""
    sp = _load("sync_prompts")

    store = tmp_path / "store"
    store.mkdir()
    (store / "tester.md").write_text("x")
    empty = tmp_path / "elsewhere"
    empty.mkdir()

    report = sp.sync_report(store, into="agents", ext=".md", mode="copy", dry=False,
                     repo=tmp_path / "repo", already=[empty])
    assert report["skipped_duplicate"] == 0, report
    assert (tmp_path / "repo" / ".github" / "agents" / "tester.agent.md").is_file()


def test_symlinking_falls_back_to_copying_where_it_is_not_permitted(tmp_path, monkeypatch):
    """Windows needs Developer Mode or admin rights to create a symlink. A teammate there should get
    a working overlay, not a traceback — the link is an optimisation, the file is the point."""
    sp = _load("sync_prompts")

    src = tmp_path / "a.md"
    src.write_text("x")
    dst = tmp_path / "out" / "a.agent.md"

    def refuse(*_a, **_k):
        raise OSError("symlink not permitted")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    action = sp._materialize(src, dst, "symlink", dry=False)
    assert dst.is_file() and dst.read_text() == "x"
    assert action == "copy", f"fell back silently as {action!r}"


def test_a_duplicate_already_in_the_overlay_is_REMOVED_not_merely_skipped(tmp_path):
    """Skipping stops it getting worse; removing is what fixes the editor.

    Alan's overlay already held 31 links, every one of them also served by his own prompts store —
    so every agent listed twice. A sync that merely declines to re-create them leaves the doubles
    exactly where they are.
    """
    sp = _load("sync_prompts")
    store = tmp_path / "store"
    store.mkdir()
    (store / "tester.md").write_text("x")
    stale = tmp_path / "repo" / ".github" / "agents" / "tester.agent.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("the duplicate the editor is listing twice")

    sp.sync_report(store, into="agents", ext=".md", mode="copy", dry=False,
                   repo=tmp_path / "repo", already=[store])
    assert not stale.exists(), "the duplicate is still there, so the editor still shows two"


def test_a_dry_run_removes_nothing(tmp_path):
    sp = _load("sync_prompts")
    store = tmp_path / "store"
    store.mkdir()
    (store / "tester.md").write_text("x")
    stale = tmp_path / "repo" / ".github" / "agents" / "tester.agent.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("still here")

    sp.sync_report(store, into="agents", ext=".md", mode="copy", dry=True,
                   repo=tmp_path / "repo", already=[store])
    assert stale.read_text() == "still here"
