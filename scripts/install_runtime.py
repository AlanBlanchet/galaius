"""Install locally built wheels as a new runtime and make it active (a developer's own build).

Every supervised long-lived process (`interact mcp`, `interact machine connect`, the TUI) moves to
it at its next quiet moment; a launcher linked into the store (`~/.local/bin/interact`) follows the
active runtime, so new launches start on it too. The
release identity is read from the checkout the wheels were built from (its HEAD commit time), so
the newest-ever floor stays meaningful; an older build is still activated (your local choice).
"""

import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from interact.upgrade.release import BuildIdentity, ReleaseFile
from interact.upgrade.store import RuntimeStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-wheel', type=Path, required=True)
    parser.add_argument('--core-wheel', type=Path, required=True)
    parser.add_argument('--checkout', type=Path, default=Path(__file__).resolve().parents[1], help='where the interact wheel was built from')
    parser.add_argument('--python', default=None, help='interpreter version for the runtime (default: this one)')
    args = parser.parse_args()
    public, core = args.public_wheel.resolve(strict=True), args.core_wheel.resolve(strict=True)
    commit, moment = subprocess.check_output(['git', '-C', str(args.checkout), 'log', '-1', '--format=%H %cI'], text=True).split()
    build = BuildIdentity(version=ReleaseFile.of(public).version, released_at=datetime.fromisoformat(moment), commit=commit)
    store = RuntimeStore.default()
    runtime = store.install({'interact': public, 'interact-core': core}, None, build, 'local', args.python)
    store.activate(runtime, explicit=True)
    print(f'{runtime.label()} active at {runtime.path}; running processes move to it at their next quiet moment', flush=True)


if __name__ == '__main__':
    main()
