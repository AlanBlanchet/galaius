"""One release check: is there a newer signed release, and if so install it beside the running one
and make it active. Run by a supervisor from the ACTIVE runtime (`interact upgrade check
--background`), so the newest code always does the fetching; a person runs it with `interact
upgrade check`.

Refused, and said so in `events.jsonl`: a document no shipped key signed, a file whose sha256 is
not the signed one, a release older than the newest this computer ran (never a silent
downgrade), an expired one, a build that already failed to start here."""

import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from interact.config.settings import Config
from interact.config.user import UserConfig
from interact.file_lock import exclusive
from interact.machines import MachineRunner
from interact.private_files import PRIVATE_FILES
from interact.upgrade.release import BuildIdentity, Release, ReleaseOrder, ReleaseRefused
from interact.upgrade.source import ReleaseKeys, ReleaseSource
from interact.upgrade.store import Runtime, RuntimeStore


class UpgradeCheck(BaseModel):
    """`config` carries the local-only settings (`auto_upgrade`, `upgrade_pin`,
    `upgrade_check_seconds`, `upgrade_github`)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    store: RuntimeStore
    keys: ReleaseKeys
    config: Config
    #: A pin names one build by its commit: 7 to 40 lowercase hex characters.
    pin_pattern: ClassVar[re.Pattern[str]] = re.compile(r"[0-9a-f]{7,40}")

    @classmethod
    def configured(cls) -> "UpgradeCheck":
        UserConfig.apply(portable=False)
        store = RuntimeStore.default()
        store.retire(ReleaseKeys.retired_by_package())
        return cls(store=store, keys=ReleaseKeys.shipped(store.retired()), config=Config())

    @staticmethod
    def server() -> str | None:
        """The Interact server this computer signed in to: the one `interact login` remembers,
        else the one its machine connection is enrolled with (a computer set up before logins
        were remembered)."""
        runner = MachineRunner()
        try:
            return (runner.config_path.parent / "login-server").read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            pass
        try:
            return runner.load().server_url
        except (OSError, ValueError, KeyError):
            return None

    def source(self) -> ReleaseSource | None:
        """ONE source per install: the signed-in server; GitHub only when no server is set up and
        the person turned it on (never because the server failed to answer)."""
        server = self.server()
        if server is not None:
            return ReleaseSource.server(server)
        return ReleaseSource.github() if self.config.upgrade_github else None

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """One check at a time on this computer (the next one then finds the work done)."""
        PRIVATE_FILES.directory(self.store.root)
        with exclusive(os.open(self.store.root / "check.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)):
            yield

    def floor(self) -> ReleaseOrder | None:
        """Nothing older than this is installed unasked: the newest build this computer ever ran
        (a bootstrap install's own build counts before the store has one)."""
        floor = self.store.pointer().floor
        own = BuildIdentity.installed() if self.store.active().path == Runtime.own().path else None
        known = [order for order in (floor, own) if order is not None]
        return max(known, key=lambda order: order.key) if known else None

    def running(self, release: Release) -> bool:
        """The active runtime IS this release: installed from it, or the bootstrap install built from
        its commit (a server installer's wheels carry their build; a GitHub one names its commit)."""
        active = self.store.active()
        if (receipt := active.receipt()) is not None:
            return receipt.identity == release.identity
        return active.path == Runtime.own().path and BuildIdentity.installed_commit() == release.commit

    def run(self) -> str:
        """What happened, in a sentence (recorded when it changed something or refused)."""
        self.store.schedule(self.config.upgrade_check_seconds)
        if not self.config.auto_upgrade:
            return "automatic upgrades are off (interact upgrade on)"
        pin = self.config.upgrade_pin.strip()
        if pin and not self.pin_pattern.fullmatch(pin):
            self.refused(f"upgrade_pin {pin!r} is not a commit (7 to 40 hex characters): ignored; interact upgrade pin <commit> sets one")
            pin = ""
        try:
            source = self.source()
        except ValueError as error:
            return self.refused(f"the remembered Interact server is not a release source: {error}")
        if source is None:
            return "no Interact server set up (interact login), so no release to check"
        with self.exclusive(), source.client() as http:
            try:
                release = source.latest(http, self.keys.without(self.store.retired()))
            except ReleaseRefused as error:
                return self.refused(f"{source.base}: {error}")
            except ValidationError as error:
                return self.refused(f"{source.base}: a signed document that is not a release ({error.error_count()} problems)")
            except httpx.HTTPStatusError as error:
                # 404 is the server answering that it has published no release, which reads very
                # differently from one that is failing: say which it is, or this looks like a bug.
                nothing = " (it has published none yet)" if error.response.status_code == 404 else ""
                return f"{source.base} offers no release{nothing} (HTTP {error.response.status_code}); trying again in {self.config.upgrade_check_seconds} s"
            except httpx.HTTPError as error:
                return f"{source.base} did not answer ({type(error).__name__}); trying again in {self.config.upgrade_check_seconds} s"
            if self.running(release):
                return f"up to date ({release.label()})"
            pinned = bool(pin) and release.commit.startswith(pin)
            if pin and not pinned:
                return f"holding the pinned build {pin}; {source.kind} offers {release.label()}"
            if release.expired():
                return self.refused(f"{release.label()} expired on {release.expires_at:%Y-%m-%d}: the server has not published since")
            if not pinned and (floor := self.floor()) is not None and release < floor:
                return self.refused(f"{source.kind} offers {release.label()}, older than {floor.label()} which this computer already ran")
            if self.store.failed(release.identity):
                return f"{release.label()} failed to start here before; waiting for a newer release"
            try:
                runtime = self.store.installed(release.identity) or self.install(source, http, release)
            except ReleaseRefused as error:
                return self.refused(f"{release.label()}: {error}")
            except (RuntimeError, OSError, httpx.HTTPError) as error:
                return self.refused(f"{release.label()} did not install: {error}")
            self.store.activate(runtime, explicit=pinned)
            self.store.prune()
        return f"{release.label()} installed and active; running processes move to it at their next quiet moment"

    def install(self, source: ReleaseSource, http: httpx.Client, release: Release) -> Runtime:
        PRIVATE_FILES.directory(self.store.root)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=self.store.root) as folder:
            staging = Path(folder)
            wheels = {wheel.name: source.download(http, wheel, staging) for wheel in release.wheels}
            lock = source.download(http, release.lock, staging)
            return self.store.install(wheels, lock, BuildIdentity.of(release), source.kind)

    def refused(self, text: str) -> str:
        """Recorded once per distinct reason (a check every few minutes never floods the log)."""
        last = self.store.events(1)
        if not last or last[0].text != text:
            self.store.record("refused", text)
        return text
