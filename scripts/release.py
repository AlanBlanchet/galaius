"""Build a client release from source snapshots (run by the server's deploy, never on a worktree).

    release.py build --source <galaius snapshot> --core <galaius-core snapshot> --commit <sha> --released-at <iso> --out <folder> [--key <pem>]

Stamps the snapshot's `galaius/data/build.json`, builds both wheels, exports the snapshot's uv.lock
with hashes (galaius-core comes as its wheel, not from the lock) and writes `release.json`: every
file a client verifies before installing. The release key lives on the release host only and signs
there; `--key` signs here, for tests with their own key.
"""

import argparse
import shutil
import subprocess
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from galaius.upgrade.release import BuildIdentity, Release
from galaius.upgrade.source import ReleaseSigner


class Publisher:
    """One release folder built from two source snapshots."""

    lifetime = timedelta(days=60)

    def __init__(self, source: Path, core: Path, out: Path) -> None:
        self.source, self.core, self.out = source, core, out
        self.uv = shutil.which("uv") or "uv"

    def wheel(self, project: Path) -> Path:
        before = set(self.out.glob("*.whl"))
        subprocess.run([self.uv, "build", "--wheel", "--quiet", "--out-dir", str(self.out), str(project)], check=True)
        (built,) = set(self.out.glob("*.whl")) - before
        return built

    def lock(self) -> Path:
        """The snapshot's resolved dependencies with their hashes, minus the projects we ship as wheels."""
        exported = subprocess.run([self.uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--no-header"],
                                  cwd=self.source, check=True, capture_output=True, text=True).stdout
        kept, skipping = [], False
        for line in exported.splitlines():
            if line and not line.startswith((" ", "#")):
                skipping = line.split()[0].split("==")[0].split("@")[0].strip().lower() in {"galaius", "galaius-core"}
            if not skipping:
                kept.append(line)
        path = self.out / "requirements.lock"
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return path

    def build(self, commit: str, released_at: datetime, signer: ReleaseSigner | None) -> Release:
        self.out.mkdir(parents=True, exist_ok=True)
        stamped = self.source / "src" / "galaius" / "data" / BuildIdentity.path
        version = tomllib.loads((self.source / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        stamped.write_text(BuildIdentity(version=version, released_at=released_at, commit=commit).model_dump_json() + "\n", encoding="utf-8")
        wheels = (self.wheel(self.source), self.wheel(self.core))
        release = Release.described(wheels, self.lock(), commit, released_at, self.lifetime)
        (self.out / "release.json").write_bytes(release.document())
        if signer is not None:
            (self.out / "release.json.sig").write_bytes(signer.signature(release.document()))
        return release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--core", type=Path, required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--released-at", type=datetime.fromisoformat, required=True)
    build.add_argument("--key", type=Path)
    build.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    release = Publisher(args.source, args.core, args.out).build(args.commit, args.released_at, ReleaseSigner.load(args.key) if args.key else None)
    print(f"release {release.label()} {release.commit[:12]} into {args.out}" + (" (signed)" if args.key else " (unsigned: signed on the release host)"))


if __name__ == "__main__":
    main()
