"""Run by an INSTALLED interact's own interpreter (CI, right after an installer): offered a signed
release built from the very commit it was installed from, the install says it is up to date and
installs nothing. The release is signed by a key made here for the run (the real release key never
leaves the Raspberry), trusted by this check only; everything else is the installed code."""

import http.server
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from interact import installed_version
from interact.config.settings import Config
from interact.upgrade.check import UpgradeCheck
from interact.upgrade.release import BuildIdentity, Release, ReleaseFile
from interact.upgrade.source import ReleaseSigner, ReleaseSource
from interact.upgrade.store import Runtime, RuntimeStore


class LocalCheck(UpgradeCheck):
    """The installed check, pointed at this run's own release server."""

    base: str

    def source(self) -> ReleaseSource:
        return ReleaseSource(base=self.base, kind="server")


def main() -> None:
    commit = BuildIdentity.installed_commit()
    if commit is None:
        sys.exit(f"this install ({Runtime.own().path}) does not know the commit it was built from")
    signer, now = ReleaseSigner.generated(), datetime.now(UTC)
    files = tuple(ReleaseFile(name=name, version=installed_version(), sha256="0" * 64, file=f"{name.replace('-', '_')}-{installed_version()}-py3-none-any.whl")
                  for name in Release.packages)
    release = Release(version=installed_version(), released_at=now, commit=commit, expires_at=now + timedelta(days=60), wheels=files,
                      lock=ReleaseFile(name="requirements.lock", sha256="0" * 64, file="requirements.lock"))
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        served = root / "install" / "release"
        served.mkdir(parents=True)
        document, signature = signer.sign(release)
        (served / "release.json").write_bytes(document)
        (served / "release.json.sig").write_bytes(signature)
        handler = type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *_: None})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: handler(*args, directory=str(root)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        store = RuntimeStore(root=root / "runtimes")
        check = LocalCheck(store=store, keys=signer.keys(), config=Config(auto_upgrade=True, upgrade_pin="", upgrade_check_seconds=300, upgrade_github=False),
                           base=f"http://127.0.0.1:{server.server_port}/install/release")
        said = check.run()
        server.shutdown()
        installed = [path.name for path in store.root.iterdir() if path.is_dir() and not path.name.startswith(".") and path.name != "live"] if store.root.is_dir() else []
    print(f"built from {commit[:12]}: {said}; runtimes installed: {installed or 'none'}")
    if not said.startswith("up to date") or installed:
        sys.exit("a fresh install re-installed (or tried to) its own build")


if __name__ == "__main__":
    main()
