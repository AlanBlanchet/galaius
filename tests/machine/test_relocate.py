"""Projects an earlier Galaius put on a PC move to ~/.galaius/projects with every record naming them
(owner 2026-11-17 « We should natively be able to do things from the ~/.galaius official install folder ! ... Projects should be
created in there !!!! »)."""

import os
from pathlib import Path

import pytest
from galaius_core import PROJECTS_ROOT

from galaius.agents import registry as reg
from galaius.agents.providers import ClaudeCodeProvider
from galaius.isolation.fence import FenceSpec
from galaius.machine.projects import relocate


def _layout(home: Path) -> tuple[Path, Path, Path]:
    hidden, earlier = home / "galaius" / "projects", home / "dev" / "galaius" / "projets"
    for folder in (hidden / "aino", earlier / "notes", earlier / "shop", earlier / "shop-mods"):
        folder.mkdir(parents=True)
        (folder / "file.md").write_text("x")
    claude = home / ".claude" / "projects"
    for folder in (hidden / "aino", earlier / "notes", earlier / "notes" / "sub", earlier / "shop-mods"):
        (ClaudeCodeProvider.project_folder(home / ".claude", folder)).mkdir(parents=True, exist_ok=True)
        (ClaudeCodeProvider.project_folder(home / ".claude", folder) / "s.jsonl").write_text("{}")
    return hidden, earlier, claude


@pytest.mark.parametrize("case", ["all_idle", "one_in_use", "target_taken", "sibling_in_use", "link_inside"])
def test_earlier_projects_move_home_with_their_conversations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)   # this PC's own login folder: ~/.claude
    home = tmp_path
    hidden, earlier, claude = _layout(home)
    new = home / ".galaius" / "projects"
    reg.save_run(reg.AgentRun(run_id="r-notes", provider="claude", name="a", cwd=str(earlier / "notes" / "sub"), started_at=1.0, exit_code=0))
    reg.save_run(reg.AgentRun(run_id="r-shop", provider="claude", name="b", cwd=str(earlier / "shop"), started_at=1.0,
                              **({"pid": os.getpid()} if case == "one_in_use" else {"exit_code": 0})))
    reg.save_run(reg.AgentRun(run_id="r-mods", provider="claude", name="c", cwd=str(earlier / "shop-mods"), started_at=1.0,
                              **({"pid": os.getpid()} if case == "sibling_in_use" else {"exit_code": 0})))
    if case == "link_inside":
        (earlier / "elsewhere").symlink_to(home / "dev")
    if case == "target_taken":
        (new / "shop").mkdir(parents=True)
    agent_roots = ("dev", relocate.EARLIER_AGENT_ROOT)
    plan = relocate.plan(home / "dev", agent_roots)
    names = {move.source.name for move in plan.moves}
    assert names == {"aino", "notes"} | {name for name in ("shop", "shop-mods") if case in ("all_idle", "link_inside") or (case == "sibling_in_use" and name == "shop")
                                          or (case in ("one_in_use", "target_taken") and name == "shop-mods")}
    assert not new.joinpath("aino").exists() and (hidden / "aino").is_dir()   # the dry run writes nothing
    rehomed, dropped = [], []
    failed = relocate.apply(plan, lambda root, moved: rehomed.append((root, moved)), dropped.append)
    assert failed == [] and (new / "aino" / "file.md").read_text() == "x"
    assert not hidden.exists()   # emptied, the visible folder of an earlier Galaius goes with its last project (one holding anything else stays: the busy test)
    assert (new / "notes" / "file.md").is_file() and not (earlier / "notes").exists()
    assert reg.get_run("r-notes").cwd == str(new / "notes" / "sub")
    assert (ClaudeCodeProvider.project_folder(home / ".claude", new / "notes" / "sub") / "s.jsonl").is_file()
    assert (ClaudeCodeProvider.project_folder(home / ".claude", new / "aino") / "s.jsonl").is_file()
    assert not ClaudeCodeProvider.project_folder(home / ".claude", earlier / "notes").exists()
    assert rehomed and rehomed[0][0] == relocate.EARLIER_AGENT_ROOT and "notes" in rehomed[0][1]
    stays = {"shop": case in ("one_in_use", "target_taken"), "shop-mods": case == "sibling_in_use"}
    for name, run in (("shop", "r-shop"), ("shop-mods", "r-mods")):
        assert (earlier / name).is_dir() == stays[name] and reg.get_run(run).cwd == str((earlier if stays[name] else new) / name)
    # `shop` is a prefix of `shop-mods`: its session folder follows its own folder only (tester r3 F2a)
    mods = new if not stays["shop-mods"] else earlier
    assert (ClaudeCodeProvider.project_folder(home / ".claude", mods / "shop-mods") / "s.jsonl").is_file()
    emptied = case == "all_idle"   # a folder in use or a link left inside keeps the earlier agent folder (tester r3 F2b)
    assert dropped == ([(relocate.EARLIER_AGENT_ROOT,)] if emptied else [])
    assert PROJECTS_ROOT not in {root for root, _ in rehomed}   # the token names the new folder by itself


def _idle(home: Path, name: str, folder: Path) -> None:
    reg.save_run(reg.AgentRun(run_id=name, provider="claude", name=name, cwd=str(folder), started_at=1.0, exit_code=0))


def test_a_source_that_is_the_projects_folder_moves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".galaius" / "projects" / "aino").mkdir(parents=True)
    assert relocate.plan(tmp_path, ()) == relocate.MovePlan()
    link = tmp_path / "galaius"   # the earlier folder reaching the target by a link: the same folder, never a source
    link.mkdir()
    (link / "projects").symlink_to(tmp_path / ".galaius" / "projects")
    assert relocate.plan(tmp_path, ()).moves == ()


def test_a_hidden_entry_a_link_and_a_running_job_keep_their_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    earlier = tmp_path / "galaius" / "projects"
    for name in ("aino", ".1234.partial"):
        (earlier / name).mkdir(parents=True)
    (earlier / "elsewhere").symlink_to(tmp_path)
    plan = relocate.plan(tmp_path, ())
    assert {move.source.name for move in plan.moves} == {"aino"}
    assert {kept.folder.name for kept in plan.kept} == {".1234.partial", "elsewhere"}
    busy = relocate.plan(tmp_path, (), busy=True)
    assert busy.moves == () and {kept.folder.name for kept in busy.kept} == {"aino", ".1234.partial", "elsewhere"}
    assert relocate.apply(busy, lambda *_: None, lambda _: None) == [] and (earlier / "aino").is_dir()


@pytest.mark.parametrize("linked", ["earlier_home", "projects_parent", "projects"])
def test_a_link_on_the_way_to_either_folder_moves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if linked == "earlier_home":   # `~/galaius` moved to another disk and linked back
        (tmp_path / "galaius").symlink_to(elsewhere)
        (elsewhere / "projects" / "aino").mkdir(parents=True)
    else:
        (tmp_path / "galaius" / "projects" / "aino").mkdir(parents=True)
        if linked == "projects_parent":
            (tmp_path / ".galaius").symlink_to(elsewhere)
        else:
            (tmp_path / ".galaius").mkdir()
            (tmp_path / ".galaius" / "projects").symlink_to(elsewhere)
    plan = relocate.plan(tmp_path, ())
    assert plan.moves == () and plan.kept
    assert relocate.apply(plan, lambda *_: None, lambda _: None) == []
    assert not (tmp_path / ".galaius" / "projects" / "aino").exists() and not (elsewhere / "aino").exists()


def test_an_existing_empty_target_name_is_never_replaced_even_when_it_appears_after_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    earlier, new = tmp_path / "galaius" / "projects", tmp_path / ".galaius" / "projects"
    (earlier / "aino").mkdir(parents=True)
    (earlier / "aino" / "keep.md").write_text("x")
    new.mkdir(parents=True)   # this PC's empty projects folder of before: the move is per folder, never the folder itself
    plan = relocate.plan(tmp_path, ())
    (new / "aino").mkdir()   # a clone landed here between the plan and the move
    failed = relocate.apply(plan, lambda *_: None, lambda _: None)
    assert len(failed) == 1 and (earlier / "aino" / "keep.md").is_file() and list((new / "aino").iterdir()) == []


def test_a_moved_run_keeps_its_conversation_with_the_folder_as_its_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    earlier, new = tmp_path / "dev" / "galaius" / "projets", tmp_path / ".galaius" / "projects"
    (earlier / "notes" / "sub").mkdir(parents=True)
    fence = FenceSpec(working_directory=tmp_path / "dev", levels={"galaius/projets/notes": "write", "galaius/other": "read"}, start=earlier / "notes" / "sub",
                      state=tmp_path / "state")
    reg.save_run(reg.AgentRun(run_id="r-notes", provider="claude", name="a", cwd=str(earlier / "notes" / "sub"), started_at=1.0, exit_code=0, fence=fence))
    plan = relocate.plan(tmp_path / "dev", ("dev", relocate.EARLIER_AGENT_ROOT))
    assert relocate.apply(plan, lambda *_: None, lambda _: None) == []
    moved = reg.get_run("r-notes").fence
    assert moved.project == new / "notes" and moved.start == new / "notes" / "sub" and moved.levels == {"galaius/other": "read"}
    assert moved.places().reach(("galaius", "other")) == "read" and not any(part.startswith(".") for part in moved.levels)


def _session_setup(home: Path) -> tuple[Path, Path, Path, Path]:
    """One earlier project `notes`, its session folder, and the owner's file a planted link would reach."""
    earlier, new = home / "galaius" / "projects", home / ".galaius" / "projects"
    (earlier / "notes").mkdir(parents=True)
    claude = home / ".claude"
    source = ClaudeCodeProvider.project_folder(claude, earlier / "notes")
    source.mkdir(parents=True)
    (source / "s.jsonl").write_text("{}")
    owner = home / ".ssh"
    owner.mkdir()
    (owner / "id_ed25519").write_text("KEY")
    return earlier, new, source, ClaudeCodeProvider.project_folder(claude, new / "notes")


@pytest.mark.parametrize("planted", ["source_link", "target_link", "target_dangling", "link_inside_source", "dir_link_inside_target"])
def test_a_link_planted_in_a_session_folder_is_never_followed_nor_moved_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str) -> None:
    """Threat review 2026-11-20 N1 / N2: a fenced run writes its own session folder. Planted links reached `~/.ssh` (moved into the
    agent's folder) and `~/.config/autostart` (a file written where it runs at login); a dangling one aborted the whole connect step."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    earlier, new, source, target = _session_setup(tmp_path)
    autostart = tmp_path / ".config" / "autostart"
    autostart.mkdir(parents=True)
    if planted == "source_link":
        source.rename(tmp_path / "linked-source")
        source.symlink_to(tmp_path / ".ssh")
    elif planted == "target_link":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(autostart)
    elif planted == "target_dangling":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(tmp_path / "nowhere")
    elif planted == "link_inside_source":
        (source / "key").symlink_to(tmp_path / ".ssh" / "id_ed25519")
        target.mkdir(parents=True)
    else:
        target.mkdir(parents=True)
        (target / "sub").symlink_to(autostart)
        (source / "sub").mkdir()
        (source / "sub" / "evil.desktop").write_text("[Desktop Entry]")
    plan = relocate.plan(tmp_path, ())
    rehomed = []
    failed = relocate.apply(plan, lambda root, names: rehomed.append(root), lambda _: None)
    assert (new / "notes").is_dir()   # the folder itself moved whatever the sessions did
    assert (tmp_path / ".ssh" / "id_ed25519").read_text() == "KEY" and list(autostart.iterdir()) == []
    assert bool(failed) == (planted != "source_link")   # a link met is named as left, never raised; a link as the source itself is kept by the plan
    assert rehomed == [] and plan.moves   # (the token root needs no rehome)


def test_a_session_folder_that_cannot_be_renamed_is_named_and_the_rest_still_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tester 2026-11-20 F1: a rename failing on a session folder (name too long once `.galaius` adds a dash, EXDEV) raised out of `apply`,
    which never reached the records, the rehome nor the drop."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    earlier, new, source, target = _session_setup(tmp_path)
    real = os.rename

    def failing(src, dst):
        if str(src) == str(source):
            raise OSError(18, "Invalid cross-device link")
        return real(src, dst)
    monkeypatch.setattr(os, "rename", failing)
    reg.save_run(reg.AgentRun(run_id="r-notes", provider="claude", name="a", cwd=str(earlier / "notes"), started_at=1.0, exit_code=0))
    plan = relocate.plan(tmp_path, ())
    failed = relocate.apply(plan, lambda *_: None, lambda _: None)
    assert len(failed) == 1 and "cross-device" in failed[0] and source.is_dir()
    assert reg.get_run("r-notes").cwd == str(new / "notes")   # the records followed the folder


def test_two_earlier_homes_holding_one_name_plan_one_move_and_name_the_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    for folder in (tmp_path / "galaius" / "projects" / "aino", tmp_path / "dev" / "galaius" / "projets" / "aino"):
        folder.mkdir(parents=True)
    plan = relocate.plan(tmp_path / "dev", ("dev", relocate.EARLIER_AGENT_ROOT))
    assert [move.source for move in plan.moves] == [tmp_path / "galaius" / "projects" / "aino"]
    assert any(kept.folder == tmp_path / "dev" / "galaius" / "projets" / "aino" and "first" in kept.why for kept in plan.kept)
    assert relocate.apply(plan, lambda *_: None, lambda _: None) == []   # the dry run promised nothing that then fails


def test_a_source_swapped_for_a_link_after_the_plan_is_never_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    earlier = tmp_path / "galaius" / "projects"
    (earlier / "aino").mkdir(parents=True)
    plan = relocate.plan(tmp_path, ())
    (earlier / "aino").rmdir()
    (earlier / "aino").symlink_to(tmp_path)
    failed = relocate.apply(plan, lambda *_: None, lambda _: None)
    assert len(failed) == 1 and not (tmp_path / ".galaius" / "projects" / "aino").exists()
