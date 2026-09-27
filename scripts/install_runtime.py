"""Install verified wheels beside a running Interact process, then switch future launches.

The existing environment is retained. Running MCP clients keep their process until their
owner reconnects; this command never signals them or reloads an editor.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile


def wheel_identity(path: Path, expected: str) -> dict[str, str]:
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError('wheel must contain exactly one package metadata record')
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if metadata['Name'] != expected or not metadata['Version']:
        raise ValueError(f'expected {expected} wheel')
    return {'name': expected, 'version': metadata['Version'], 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-wheel', type=Path, required=True)
    parser.add_argument('--core-wheel', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, default=Path.home() / '.local/share/interact/runtimes')
    parser.add_argument('--launcher', type=Path, default=Path.home() / '.local/bin/interact')
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    public, core = args.public_wheel.resolve(strict=True), args.core_wheel.resolve(strict=True)
    packages = [wheel_identity(public, 'interact'), wheel_identity(core, 'interact-core')]
    uv = shutil.which('uv')
    if uv is None:
        parser.error('uv is required')
    launcher = args.launcher.absolute()
    if launcher.exists() and not launcher.is_symlink():
        parser.error('launcher is an unmanaged regular file; preserve it and choose a separate launcher')
    previous = os.readlink(launcher) if launcher.is_symlink() else None
    destination = args.runtime_root.absolute() / (
        packages[0]['version'] + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([uv, 'venv', '--python', args.python, str(destination)], check=True)
    python = destination / 'bin/python'
    override = destination / 'core-override.txt'
    override.write_text(f'interact-core @ {core.as_uri()}\n')
    subprocess.run([uv, 'pip', 'install', '--python', str(python), '--overrides', str(override), str(public)], check=True)
    probe = subprocess.check_output([str(python), '-c',
        'import json,interact,interact_core; from importlib.metadata import version; '
        'print(json.dumps({"interact":version("interact"),"interact-core":version("interact-core"),'
        '"source":interact.__file__,"core_source":interact_core.__file__}))'
    ], text=True)
    loaded = json.loads(probe)
    if any(loaded[item['name']] != item['version'] for item in packages):
        raise RuntimeError('installed package versions differ from supplied wheels; launcher unchanged')
    if any(not Path(loaded[name]).resolve().is_relative_to(destination) for name in ('source', 'core_source')):
        raise RuntimeError('runtime imports outside its installed environment; launcher unchanged')
    executable = destination / 'bin/interact'
    if not executable.is_file():
        raise RuntimeError('installed wheel has no Interact entry point; launcher unchanged')
    receipt = {'runtime': str(destination), 'launcher': str(launcher), 'previous_launcher_target': previous,
               'packages': packages, 'loaded': loaded, 'running_processes_restarted': False}
    (destination / 'installation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    launcher.parent.mkdir(parents=True, exist_ok=True)
    activate_launcher(launcher, executable, previous)
    print(json.dumps(receipt), flush=True)


def activate_launcher(launcher: Path, executable: Path, previous: str | None) -> None:
    """Use no-clobber creation; a concurrent launcher is never overwritten.

    Moving the old link aside leaves a brief gap for *new* launches. Existing processes
    keep running, and every displaced entry remains recoverable if activation conflicts.
    """
    preserved = launcher.with_name(f'.{launcher.name}-previous-{uuid4().hex}')
    moved = False
    try:
        if previous is not None:
            os.rename(launcher, preserved)
            moved = True
            if not preserved.is_symlink() or os.readlink(preserved) != previous:
                raise RuntimeError('launcher changed during installation')
        # symlink creation fails atomically if any other installer owns this path now.
        launcher.symlink_to(executable)
    except BaseException as error:
        if moved:
            try:
                # Link the entry itself, not its target. This also refuses an occupied path.
                os.link(preserved, launcher, follow_symlinks=False)
            except OSError:
                raise RuntimeError(f'activation conflict; concurrent launcher preserved, displaced entry retained at {preserved}') from error
            preserved.unlink()
        raise
    if moved:
        preserved.unlink()


if __name__ == '__main__':
    main()
