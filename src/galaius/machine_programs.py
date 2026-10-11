"""The agent programs (Claude Code, Codex) this computer's owner installs from the web
(`AgentProgramInstallRequest`): its vendor's own install script, fetched from the address the
provider declares (`AgentProvider.installers`, never one the server sends), run for this user with
a system-only environment, then the program's own sign-in, waited for until its status command
says it is signed in. A subscription's sign-in (`subscription_id`) runs the same way inside that
subscription's own folder (`Subscription`), never touching the default login: one job per program
and login at a time; what each stands at is `AgentProviderState`."""

import asyncio
import logging
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx
from galaius_core import DEVICE_CODE, AgentProgramFailure, AgentProgramStep, AgentProvider, AgentProviderState, AgentSignIn
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from galaius.agents.providers import PROVIDERS, AgentProvider as Program, ProgramInstaller
from galaius.agents.subscriptions import Subscription
from galaius.processes import end_process_tree, process_group_options

logger = logging.getLogger(__name__)


class ProgramFailed(Exception):
    """An install or sign-in stopped; `code` is what the server words for the owner."""

    def __init__(self, code: AgentProgramFailure, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class AgentPrograms(BaseModel):
    """What each agent program here stands at, and the one job (install, then sign-in) per program
    the web started. `environment` is what every program it runs gets: system variables only, never
    the machine's token or a project's secrets (threat M3)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    environment: dict[str, str]
    #: Where installer output goes (never a sign-in's: it carries one-time codes, threat M4).
    log_path: Path
    #: Whether this provider may run agents here (the owner's switch, `AgentProviderState.active`).
    active: Callable[[str], bool] = Field(default=lambda _name: True)
    #: How downloads reach the network (a test's own transport; None: the system's).
    transport: httpx.BaseTransport | None = None
    install_seconds: ClassVar[float] = 600
    sign_in_seconds: ClassVar[float] = 15 * 60
    poll_seconds: ClassVar[float] = 3
    script_bytes: ClassVar[int] = 2 * 1024 * 1024
    redirects: ClassVar[int] = 5
    CODE: ClassVar[re.Pattern[str]] = re.compile(DEVICE_CODE)
    ANSI: ClassVar[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*m")
    #: The steps a job is under way in: asked again, it is not started twice (threat M6).
    RUNNING: ClassVar[frozenset[str]] = frozenset({"installing", "signing_in"})

    #: Each job by its program and login (a subscription's id; None: the default login).
    _jobs: dict[tuple[str, UUID | None], AgentProviderState] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @staticmethod
    def installable() -> tuple[Program, ...]:
        return tuple(provider for provider in PROVIDERS.values() if provider.installer() is not None)

    def states(self) -> tuple[AgentProviderState, ...]:
        """Each installable program's default login, then each subscription signed in here: its job's
        state while it runs, else what it is now (a failed job's failure stays shown until it is
        installed and signed in), a subscription's with its last usage reading."""
        with self._lock:
            jobs = dict(self._jobs)
        logins = [(provider, None) for provider in self.installable()] + [(subscription.program, subscription) for subscription in Subscription.all()]
        return tuple(job if (job := jobs.get(self._key(provider, subscription))) is not None and job.step in self.RUNNING else self._now(provider, job, subscription)
                     for provider, subscription in logins)

    def install(self, name: AgentProvider, subscription_id: UUID | None = None) -> AgentProviderState:
        """Starts `name`'s job (inside `subscription_id`'s folder, made now when new) unless one runs or
        it is ready; its state either way."""
        provider = PROVIDERS[name]
        subscription = Subscription.create(subscription_id, name) if subscription_id is not None else None
        key = self._key(provider, subscription)
        if provider.installer() is None:
            return self._base(provider, subscription).model_copy(update={"step": "failed", "failure": "unsupported_system", "changed_at": datetime.now(UTC)})
        with self._lock:
            if (job := self._jobs.get(key)) is not None and job.step in self.RUNNING:
                return job
        now = self._now(provider, None, subscription)
        if now.step == "ready":
            return now
        with self._lock:
            if (job := self._jobs.get(key)) is not None and job.step in self.RUNNING:
                return job
            started = self._jobs[key] = now.model_copy(update={"step": "signing_in" if now.available else "installing", "changed_at": datetime.now(UTC)})
        threading.Thread(target=self._job, args=(provider, subscription), name=f"program-{name}", daemon=True).start()
        return started

    def sign_out(self, name: AgentProvider, subscription_id: UUID) -> AgentProviderState:
        """`subscription_id` signed out: its program's own sign-out inside its folder, then the folder
        removed with whatever credential it kept. The default login is never touched. Its state after."""
        subscription = Subscription.of(subscription_id)
        if subscription.provider != name:
            raise PermissionError(f"subscription {subscription_id} is a {subscription.provider} login, not {name}")
        with self._lock:
            if (job := self._jobs.get(self._key(subscription.program, subscription))) is not None and job.step in self.RUNNING:
                raise PermissionError("its sign-in is under way; sign out once it ended")
        state = self._base(subscription.program, subscription)
        self._logout(subscription)
        with self._lock:
            self._jobs.pop(self._key(subscription.program, subscription), None)
        return state.model_copy(update={"step": "signed_out", "signed_in": False, "usage": (), "changed_at": datetime.now(UTC)})

    # ---- the job -------------------------------------------------------------------------

    def _job(self, provider: Program, subscription: Subscription | None) -> None:
        key = self._key(provider, subscription)
        try:
            if not provider.available():
                self._install(provider, key)
            self._sign_in(provider, subscription)
        except ProgramFailed as failed:
            logger.warning("%s program: %s (%s)", provider.name, failed.code, failed)
            self._update(key, step="failed", failure=failed.code, detail=str(failed)[:400], sign_in=None)
        except Exception as error:  # one job's crash is its own failure, never the runner's, never a stuck step
            logger.exception("%s program job crashed", provider.name)
            self._update(key, step="failed", failure="install_failed", detail=f"{type(error).__name__}: {error}"[:400], sign_in=None)

    def _install(self, provider: Program, key: tuple[str, UUID | None]) -> None:
        installer = provider.installer()
        assert installer is not None  # `install` refuses a program without one
        with tempfile.TemporaryDirectory(prefix="galaius-program-") as folder:  # 0700 on POSIX
            script = Path(folder) / Path(urlsplit(installer.url).path).name
            script.write_bytes(self.download(installer))
            try:
                done = subprocess.run([*installer.shell, str(script)], env={**self.environment, **installer.environment}, cwd=folder,
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=self.install_seconds)
            except FileNotFoundError as error:
                raise ProgramFailed("unsupported_system", f"{installer.shell[0]} is not on this computer") from error
            except subprocess.TimeoutExpired as error:
                raise ProgramFailed("install_failed", f"the installer ran past {self.install_seconds:g} s") from error
        self._log(provider, done.stdout + done.stderr)
        if done.returncode != 0 or not provider.available():
            raise ProgramFailed("install_failed", self.last_line(done.stderr or done.stdout) or f"the installer exited {done.returncode}")
        self._update(key, available=True, version=self.version(provider))

    def download(self, installer: ProgramInstaller) -> bytes:
        """The script, over https, through `installer.hosts` only (each redirect checked), at most
        `script_bytes`; TLS checked as always (threat M3)."""
        url = installer.url
        try:
            with httpx.Client(follow_redirects=False, timeout=30, transport=self.transport) as client:
                for _hop in range(self.redirects + 1):
                    parts = urlsplit(url)
                    if parts.scheme != "https" or parts.hostname not in installer.hosts or parts.port not in (None, 443):
                        raise ProgramFailed("download_failed", f"the installer address left its vendor ({parts.scheme}://{parts.hostname})")
                    with client.stream("GET", url) as answer:
                        if answer.is_redirect:
                            url = urljoin(url, answer.headers.get("location", ""))
                            continue
                        if answer.status_code != 200:
                            raise ProgramFailed("download_failed", f"{parts.hostname} answered HTTP {answer.status_code}")
                        body = bytearray()
                        for chunk in answer.iter_bytes():
                            body += chunk
                            if len(body) > self.script_bytes:
                                raise ProgramFailed("download_failed", "the installer is larger than any install script")
                        return bytes(body)
        except httpx.HTTPError as error:
            raise ProgramFailed("download_failed", f"{type(error).__name__}: {error}"[:300]) from error
        raise ProgramFailed("download_failed", "too many redirects")

    def _sign_in(self, provider: Program, subscription: Subscription | None) -> None:
        """Its own sign-in command, kept waiting (stdin open, never written: no code is relayed)
        until its status says signed in, `sign_in_seconds` at most; Claude Code opens this
        computer's browser, Codex prints a device code the web shows. A subscription's runs inside
        its folder, and is undone when the default login no longer names the same login after it."""
        key, environment = self._key(provider, subscription), self._environment(subscription)
        signed_in, account = self._status(provider, environment)
        if signed_in:
            self._update(key, step="ready", signed_in=True, account=account, sign_in=None)
            return
        default = asyncio.run(provider.login_digest(self.environment)) if subscription is not None else ""
        deadline = datetime.now(UTC) + timedelta(seconds=self.sign_in_seconds)
        self._update(key, step="signing_in", signed_in=False, sign_in=AgentSignIn(expires_at=deadline))
        login = subprocess.Popen([provider.executable(), *provider.login_arguments], env=environment, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **process_group_options())
        threading.Thread(target=self._read_code, args=(key, login, deadline), daemon=True).start()
        try:
            while True:
                time.sleep(self.poll_seconds)
                signed_in, account = self._status(provider, environment)
                if signed_in:
                    if subscription is not None and asyncio.run(provider.login_digest(self.environment)) != default:
                        self._logout(subscription)
                        raise ProgramFailed("sign_in_failed", "the sign-in changed this computer's default login; the subscription was signed out again")
                    self._update(key, step="ready", signed_in=True, account=account, sign_in=None)
                    return
                if login.poll() is not None:
                    raise ProgramFailed("sign_in_failed", f"its sign-in ended (exit {login.returncode}) without signing in")
                if datetime.now(UTC) >= deadline:
                    raise ProgramFailed("sign_in_expired", f"not signed in within {self.sign_in_seconds / 60:.0f} min")
        finally:
            if login.poll() is None:
                end_process_tree(login.pid)
                try:
                    login.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    login.kill()

    def _read_code(self, key: tuple[str, UUID | None], login: subprocess.Popen, deadline: datetime) -> None:
        """A device code the sign-in prints, onto its state while it still waits; its output is never
        logged (threat M4)."""
        assert login.stdout is not None
        for line in login.stdout:
            word = self.ANSI.sub("", line).strip()
            if self.CODE.match(word):
                self._update(key, only_while="signing_in", sign_in=AgentSignIn(code=word, expires_at=deadline))

    # ---- states ----------------------------------------------------------------------------

    def _update(self, key: tuple[str, UUID | None], *, only_while: AgentProgramStep | None = None, **changes: object) -> None:
        """One job state change, under the lock, never running anything: a terminal state is always
        written; a change `only_while` a step is dropped once the job left it."""
        if unknown := changes.keys() - AgentProviderState.model_fields.keys():
            raise ValueError(f"not a program state field: {sorted(unknown)}")
        with self._lock:
            current = self._jobs[key]
            if only_while is not None and current.step != only_while:
                return
            self._jobs[key] = AgentProviderState.model_validate({**current.model_dump(), **changes, "changed_at": datetime.now(UTC)})

    @staticmethod
    def _key(provider: Program, subscription: Subscription | None) -> tuple[str, UUID | None]:
        return provider.name, subscription.id if subscription is not None else None

    def _environment(self, subscription: Subscription | None) -> dict[str, str]:
        """What a program runs with for this login: the system variables, its folder named to it."""
        return self.environment if subscription is None else {**self.environment, **subscription.overlay()}

    def _status(self, provider: Program, environment: dict[str, str]) -> tuple[bool | None, str]:
        return asyncio.run(provider.login_status(environment))

    def _logout(self, subscription: Subscription) -> None:
        """Its program's own sign-out inside its folder (best effort: the folder goes either way), then the folder."""
        if subscription.program.logout_arguments and subscription.program.available():
            try:
                subprocess.run([subscription.program.executable(), *subscription.program.logout_arguments], env=self._environment(subscription),
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                logger.warning("%s sign-out of a subscription did not complete; its folder is removed", subscription.provider)
        subscription.remove()

    def _base(self, provider: Program, subscription: Subscription | None = None) -> AgentProviderState:
        installed = provider.available()
        return AgentProviderState(provider=provider.name, active=self.active(provider.name), available=installed,
                                  version=self.version(provider) if installed else "",
                                  subscription_id=subscription.id if subscription is not None else None,
                                  usage=subscription.usage() if subscription is not None else ())

    def _now(self, provider: Program, last: AgentProviderState | None, subscription: Subscription | None = None) -> AgentProviderState:
        """What `provider` is here now; `last`, a finished job's failure, while it is not signed in, and
        its `changed_at` (when the web's job last moved it) while it is still ready: the server tells
        the owner of a sign-in once, by that time."""
        base = self._base(provider, subscription)
        signed_in, account = self._status(provider, self._environment(subscription)) if base.available else (False, "")
        if not signed_in and last is not None and last.step == "failed":
            return last
        step: AgentProgramStep = "missing" if not base.available else "ready" if signed_in else "signed_out"
        kept = last.changed_at if last is not None and last.step == step else None
        return base.model_copy(update={"step": step, "signed_in": signed_in if base.available else None, "account": account, "changed_at": kept})

    def version(self, provider: Program) -> str:
        try:
            done = subprocess.run([provider.executable(), "--version"], env=self.environment, capture_output=True, text=True, errors="replace",
                                  timeout=15, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return self.last_line(done.stdout)[:80]

    @classmethod
    def last_line(cls, text: str) -> str:
        lines = [cls.ANSI.sub("", line).strip() for line in text.splitlines()]
        return next((line for line in reversed(lines) if line), "")[:400]

    def _log(self, provider: Program, output: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"--- {datetime.now(UTC).isoformat()} {provider.name} install\n{output[-20_000:]}\n")
