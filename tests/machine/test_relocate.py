"""Projects an earlier Galaius put on a PC move to ~/galaius/projects with every record naming them
(owner 2026-11-11 « A normal user wouldn't have the dev project, please make the distinction »)."""

import os
from pathlib import Path

import pytest
from galaius_core import PROJECTS_ROOT

from galaius.agents import registry as reg
from galaius.agents.providers import ClaudeCodeProvider
from galaius.machine.projects import relocate


def _layout(home: Path) -> tuple[Path, Path, Path]:
    hidden, earlier = home / ".galaius" / "projects", home / "dev" / "galaius" / "projets"
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
    new = home / "galaius" / "projects"
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
