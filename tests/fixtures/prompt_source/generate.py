from pathlib import Path

for name in ("agents/a.md", "skills/s/SKILL.md", "rules/r.md"):
    target = Path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{name}\n")
Path("AGENTS.md").write_text("agents\n")
Path("instructions.md").write_text("instructions\n")
Path("org.json").write_text("{}\n")
