"""Publish a signed client release from source snapshots (run by the server's deploy, never on a worktree).

    release.py keygen --private ~/.config/interact/release-signing/<name>.pem --public src/interact/data/release-keys/<name>.pub
    release.py build --source <interact snapshot> --core <interact-core snapshot> --commit <sha> --released-at <iso> --key <pem> --out <folder>

`build` stamps the snapshot's `interact/data/build.json`, builds both wheels, exports the snapshot's
uv.lock with hashes (interact-core comes as its signed wheel, not from the lock), and writes
`release.json` + `release.json.sig` beside them: everything a client verifies before installing.
"""

import argparse
import os
import shutil
import subprocess
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from interact.upgrade.release import BuildIdentity, Release
from interact.upgrade.source import ReleaseSigner


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
                skipping = line.split()[0].split("==")[0].split("@")[0].strip().lower() in {"interact", "interact-core"}
            if not skipping:
                kept.append(line)
        path = self.out / "requirements.lock"
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return path

    def build(self, commit: str, released_at: datetime, signer: ReleaseSigner) -> Release:
        self.out.mkdir(parents=True, exist_ok=True)
        stamped = self.source / "src" / "interact" / "data" / BuildIdentity.path
        version = tomllib.loads((self.source / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        stamped.write_text(BuildIdentity(version=version, released_at=released_at, commit=commit).model_dump_json() + "\n", encoding="utf-8")
        wheels = (self.wheel(self.source), self.wheel(self.core))
        release = Release.described(wheels, self.lock(), commit, released_at, self.lifetime)
        document, signature = signer.sign(release)
        (self.out / "release.json").write_bytes(document)
        (self.out / "release.json.sig").write_bytes(signature)
        return release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen")
    keygen.add_argument("--private", type=Path, required=True)
    keygen.add_argument("--public", type=Path, required=True)
    build = commands.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--core", type=Path, required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--released-at", type=datetime.fromisoformat, required=True)
    build.add_argument("--key", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "keygen":
        if args.private.exists():
            parser.error(f"{args.private} exists: a signing key is never overwritten")
        signer = ReleaseSigner.generated()
        args.private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(args.private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(signer.private_pem())
        args.public.write_bytes(signer.public_pem())
        print(f"private key {args.private} (keep it off every repository), public key {args.public}")
        return
    release = Publisher(args.source, args.core, args.out).build(args.commit, args.released_at, ReleaseSigner.load(args.key))
    print(f"release {release.label()} {release.commit[:12]} signed into {args.out}")


if __name__ == "__main__":
    main()
