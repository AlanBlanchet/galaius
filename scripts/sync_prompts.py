#!/usr/bin/env python3
"""Materialize the current developer's agent/prompt customizations into this repo's overlay.

VS Code discovers workspace-level customizations from four `.github/` subfolders:
  .github/agents/             <name>.agent.md
  .github/instructions/       <name>.instructions.md
  .github/prompts/            <name>.prompt.md
  .github/skills/             <name>/SKILL.md

Those folders are gitignored — they are per-developer, never committed. Any developer who
clones interact points this script at THEIR OWN prompts store and gets their own set; the
default points at each user's live VS Code prompt folder (already synced from their own
source of truth, so a `skillshare`-style store only enters the picture via --source):

  python scripts/sync_prompts.py                       # from ~/.config/Code/User/prompts
  python scripts/sync_prompts.py --source repo/agents  # a specific folder of <name>.md
  python scripts/sync_prompts.py --source repo/agents --ext .md

  # a prompts checkout (Anthropic-standard layout) maps by extension:
  python scripts/sync_prompts.py --source ~/dev/my-prompts/agents --ext .md --into agents
  python scripts/sync_prompts.py --source ~/dev/my-prompts/rules  --ext .md --into instructions

--dry-run reports what would land without writing. A manifest is always written to
.github/agents/.sync-prompts.json so librarian/others can re-materialize exactly the same
overlay after editing the source store. Re-running re-syncs: symlinks are re-pointed,
copies are overwritten from the source of truth, and overlay entries with no source
counterpart are pruned (safety: pruning happens only on a non-dry run).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAY_DIRS = ("agents", "instructions", "prompts", "skills")
_MANIFEST = REPO_ROOT / ".github" / "agents" / ".sync-prompts.json"

def default_source(system: str | None = None, home: Path | None = None) -> Path:
    """This machine's VS Code user prompt folder.

    Per-platform, because interact ships on all three and its CI drives real GUI sessions on macOS
    and Windows. A hard-coded `~/.config/...` told a teammate on either that their own prompts store
    did not exist — and the project's own `vscode-sync.sh` already knew the per-platform paths, so
    this is following a convention that was already here rather than inventing one.
    """
    system = system or platform.system()
    home = home or Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Code" / "User" / "prompts"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Code" / "User" / "prompts"
    return home / ".config" / "Code" / "User" / "prompts"

# Route a VS Code flat file name (<name>.<layer>.<ext>) to its overlay folder + final name.
# Returns (overlay_dir, final_rel_name) or None to keep the file where the layer says.
_FLAT_ROUTES = {
    "agent": ("agents", lambda stem, ext: f"{stem}.agent.md"),
    "skill": ("skills", lambda stem, ext: f"{stem}/SKILL.md"),
    "prompt": ("prompts", lambda stem, ext: f"{stem}.prompt.md"),
    "instructions": ("instructions", lambda stem, ext: f"{stem}.instructions.md"),
}


def _route(src: Path, *, into: str | None, ext: str) -> tuple[str, str] | None:
    """Map one source file to (overlay subfolder, relative name). None → skip."""
    if src.suffix != ext:
        return None
    if into:  # uniform folder for a uniform source (e.g. agents/ → .github/agents/)
        return into, f"{src.stem}.{_overlay_ext(into)}" if into != "skills" else f"{src.stem}/SKILL.md"
    # Flat VS Code prompts dir: <name>.<layer>.<ext> (layer possibly multi-part, e.g. skill.instructions).
    parts = src.stem.split(".")
    layer = parts[-1] if len(parts) > 1 else None
    name = parts[0]
    if layer in _FLAT_ROUTES:
        overlay, render = _FLAT_ROUTES[layer]
        return overlay, render(src.stem[: -len(layer) - 1], ext)
    if len(parts) > 2 and parts[-2:] == ["skill", "instructions"]:  # <name>.skill.instructions.md
        return "instructions", f"{name}.skill.instructions.md"
    return None  # a flat file with no recognizable layer → skipped, not guessed


def _overlay_ext(overlay: str) -> str:
    return {"agents": "agent.md", "prompts": "prompt.md", "instructions": "instructions.md"}[overlay]


def _materialize(src: Path, dst: Path, mode: str, *, dry: bool) -> str:
    """Link (or copy) one file. Returns the action taken for the report."""
    action = "link" if mode == "symlink" else "copy"
    if dry:
        return f"would-{action}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        if dst.is_symlink() and dst.resolve() == src.resolve():
            return "ok"
        dst.unlink()
    if mode == "symlink":
        try:
            dst.symlink_to(src.resolve())
            return action
        except OSError:
            # Windows needs Developer Mode or admin rights to link. The link is an optimisation
            # (edits show through instantly); the FILE is the point, so fall back rather than
            # handing a teammate a traceback.
            pass
    shutil.copy2(src, dst)
    return "copy"


def already_served(name: str, stores: list[Path]) -> bool:
    """Whether the editor ALREADY finds this customization without the overlay.

    VS Code discovers customizations from the developer's own prompts store AND from the
    workspace's `.github`. Mirroring a store that already serves every workspace makes the editor
    find the same agent twice and list it twice — which is exactly what Alan hit: "In copilot, i
    also have double the agents for each files."

    The overlay exists so a developer WITHOUT that store gets one, not to hand a second copy to the
    developer who already has it. Matching is by STEM, because the same agent is `librarian.md` in
    a prompts checkout and `librarian.agent.md` in a flat prompts dir.
    """
    stem = name.split(".")[0]
    for store in stores:
        if not store.is_dir():
            continue
        for existing in store.iterdir():
            if existing.is_file() and existing.name.split(".")[0] == stem:
                return True
    return False


def sync(source: Path, *, into: str | None, ext: str, mode: str, dry: bool,
         repo: Path | None = None, already: list[Path] | None = None) -> int:
    """Exit-code wrapper; `sync_report` carries the detail."""
    report = sync_report(source, into=into, ext=ext, mode=mode, dry=dry, repo=repo, already=already)
    return report["exit"]


def sync_report(source: Path, *, into: str | None, ext: str, mode: str, dry: bool,
                repo: Path | None = None, already: list[Path] | None = None) -> dict:
    root = repo or REPO_ROOT
    stores = already if already is not None else []
    if not source.is_dir():
        print(f"ERROR: source {source} is not a directory", file=sys.stderr)
        return {"exit": 1, "materialized": 0, "skipped_duplicate": 0}
    files = sorted(p for p in source.iterdir() if p.is_file() and p.suffix == ext)
    if not files:
        print(f"ERROR: no *{ext} files under {source}", file=sys.stderr)
        return {"exit": 1, "materialized": 0, "skipped_duplicate": 0}

    kept: list[Path] = []
    report: list[str] = []
    duplicates = 0
    for src in files:
        routed = _route(src, into=into, ext=ext)
        if routed is None:
            report.append(f"skip    {src.name}")
            continue
        if already_served(src.name, stores):
            duplicates += 1
            # Remove it if a previous run put it here: skipping stops the problem growing, but the
            # doubles the editor is ALREADY listing only go away when the overlay copy is gone.
            overlay, rel = routed
            stale = root / ".github" / overlay / rel
            if not dry and (stale.is_symlink() or stale.exists()):
                stale.unlink()
                report.append(f" removed  .github/{overlay}/{rel} — your own store already serves it")
            else:
                report.append(f" already  {src.name} — the editor finds this without the overlay")
            continue
        overlay, rel = routed
        dst = root / ".github" / overlay / rel
        kept.append(dst)
        report.append(f"{_materialize(src, dst, mode, dry=dry):>10}  .github/{overlay}/{rel} <- {src.name}")

    if not dry:
        # Prune overlay entries with no source counterpart (only folders we actually touched).
        touched = {p.relative_to(root).parts[1] for p in kept} if kept else set()
        for overlay in touched:
            odir = root / ".github" / overlay
            for existing in sorted(odir.rglob("*")):
                if existing.is_file() and existing not in kept and existing.name != _MANIFEST.name:
                    existing.unlink()

    for line in report:
        print(line)
    print(f"\n{'would materialize' if dry else 'materialized'} {len(kept)}/{len(files)} files from {source}")
    if duplicates:
        print(f"skipped {duplicates} already served by your own prompts store — "
              "materializing them here would list every one of them twice in the editor")
    if not dry:
        manifest = root / ".github" / "agents" / ".sync-prompts.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {"source": str(source), "into": into, "ext": ext, "mode": mode,
                 "skipped_duplicate": duplicates,
                 "files": [str(p.relative_to(root)) for p in kept]},
                indent=2,
            )
            + "\n"
        )
        print(f"manifest: {manifest.relative_to(root)}")
    return {"exit": 0, "materialized": len(kept), "skipped_duplicate": duplicates}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=None,
                    help="folder of <name>.md files to materialize (default: this machine's VS Code prompts store)")
    ap.add_argument("--ext", default=".md", help="source file extension to include (default: .md)")
    ap.add_argument("--into", choices=OVERLAY_DIRS, default=None,
                    help="uniform overlay folder for a uniform source (agents/instructions/prompts/skills); "
                         "omit for a VS Code flat prompts dir, where each file's own suffix routes it")
    ap.add_argument("--mode", choices=("symlink", "copy"), default="symlink",
                    help="symlink stays live-linked to the source (default); copy snapshots it")
    ap.add_argument("--dry-run", action="store_true", help="report what would land, write nothing")
    ap.add_argument("--allow-duplicates", action="store_true",
                    help="materialize even what your own prompts store already serves the editor "
                         "(off by default: it makes every such agent appear TWICE)")
    args = ap.parse_args()
    source = args.source or default_source()
    # What the editor already finds without this overlay. Anything here is skipped, or it is listed
    # twice — VS Code reads the user's prompts store AND the workspace's .github, and does not
    # deduplicate between them.
    already: list[Path] = []
    if not args.allow_duplicates:
        already = [default_source(), Path.home() / ".claude" / "agents"]
        already = [p for p in already if p.is_dir() and p.resolve() != source.resolve()]
    return sync(source, into=args.into, ext=args.ext, mode=args.mode, dry=args.dry_run,
                already=already)


if __name__ == "__main__":
    sys.exit(main())
