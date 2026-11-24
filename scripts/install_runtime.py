"""Install locally built wheels as a new runtime and make it active (a developer's own build).

Every supervised long-lived process (`galaius mcp`, `galaius machine connect`, the TUI) moves to
it at its next quiet moment; a launcher linked into the store (`~/.local/bin/galaius`) follows the
active runtime, so new launches start on it too. The
release identity is read from the checkout the wheels were built from (its HEAD commit time), so
the newest-ever floor stays meaningful; an older build is still activated (your local choice).
"""

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path

from galaius.upgrade.release import BuildIdentity, ReleaseFile
from galaius.upgrade.store import RuntimeStore


def registry_refusal(environ: dict[str, str]) -> str | None:
    """Why a store of its own (`GALAIUS_RUNTIMES`: a test install) is refused without a registry of its own
    (`GALAIUS_AGENTS_DIR`): the tray and queue dispatchers it starts would join this computer's registry and run from
    that folder for hours (2026-11-24 19:15: two ran from /tmp). Only the release path writes the real `current.json`."""
    if environ.get("GALAIUS_RUNTIMES") and not environ.get("GALAIUS_AGENTS_DIR"):
        return ("refused: a runtime store of its own (GALAIUS_RUNTIMES) is a test install, and its processes would work in this "
                "computer's agent registry; give it a registry of its own too (GALAIUS_AGENTS_DIR=<folder>)")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-wheel', type=Path, required=True)
    parser.add_argument('--core-wheel', type=Path, required=True)
    parser.add_argument('--checkout', type=Path, default=Path(__file__).resolve().parents[1], help='where the galaius wheel was built from')
    parser.add_argument('--python', default=None, help='interpreter version for the runtime (default: this one)')
    args = parser.parse_args()
    if (refusal := registry_refusal(dict(os.environ))) is not None:
        raise SystemExit(refusal)
    public, core = args.public_wheel.resolve(strict=True), args.core_wheel.resolve(strict=True)
    git_dir, common_dir = (Path(subprocess.check_output(["git", "-C", str(args.checkout), "rev-parse", flag], text=True).strip()) for flag in ("--absolute-git-dir", "--git-common-dir"))
    if git_dir != (common_dir if common_dir.is_absolute() else args.checkout / common_dir).resolve() and not os.environ.get("GALAIUS_RUNTIMES"):
        # A linked worktree is a slice's scratch checkout: activating its build moves every process of this computer onto it (2026-11-24).
        raise SystemExit("refused: this checkout is a linked git worktree; install its build into a store of its own (GALAIUS_RUNTIMES=<folder>, with GALAIUS_AGENTS_DIR=<folder>)")
    commit, moment = subprocess.check_output(['git', '-C', str(args.checkout), 'log', '-1', '--format=%H %cI'], text=True).split()
    build = BuildIdentity(version=ReleaseFile.of(public).version, released_at=datetime.fromisoformat(moment), commit=commit)
    store = RuntimeStore.default()
    runtime = store.install({'galaius': public, 'galaius-core': core}, None, build, 'local', args.python)
    store.activate(runtime, explicit=True)
    print(f'{runtime.label()} active at {runtime.path}; running processes move to it at their next quiet moment', flush=True)


if __name__ == '__main__':
    main()
