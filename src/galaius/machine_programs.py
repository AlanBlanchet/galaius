"""The agent programs (Claude Code, Codex) this computer's owner installs from the web
(`AgentProgramInstallRequest`): its vendor's own install script, fetched from the address the
provider declares (`AgentProvider.installers`, never one the server sends), run for this user with
a system-only environment, then the program's own sign-in, waited for until its status command
says it is signed in. One job per program at a time; what each stands at is `AgentProviderState`."""

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

import httpx
from galaius_core import DEVICE_CODE, AgentProgramFailure, AgentProgramStep, AgentProvider, AgentProviderState, AgentSignIn
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from galaius.agents.providers import PROVIDERS, AgentProvider as Program, ProgramInstaller
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

    _jobs: dict[str, AgentProviderState] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @staticmethod
    def installable() -> tuple[Program, ...]:
        return tuple(provider for provider in PROVIDERS.values() if provider.installer() is not None)

    def states(self) -> tuple[AgentProviderState, ...]:
        """Each installable program: its job's state while it runs, else what it is now (a failed
        job's failure stays shown until the program is installed and signed in)."""
        with self._lock:
            jobs = dict(self._jobs)
        return tuple(job if (job := jobs.get(provider.name)) is not None and job.step in self.RUNNING else self._now(provider, job)
                     for provider in self.installable())

    def install(self, name: AgentProvider) -> AgentProviderState:
        """Starts `name`'s job unless one runs or it is ready; its state either way."""
        provider = PROVIDERS[name]
        if provider.installer() is None:
            return self._base(provider).model_copy(update={"step": "failed", "failure": "unsupported_system", "changed_at": datetime.now(UTC)})
        with self._lock:
            if (job := self._jobs.get(name)) is not None and job.step in self.RUNNING:
                return job
        now = self._now(provider, None)
        if now.step == "ready":
            return now
        with self._lock:
            if (job := self._jobs.get(name)) is not None and job.step in self.RUNNING:
                return job
            started = self._jobs[name] = now.model_copy(update={"step": "signing_in" if now.available else "installing", "changed_at": datetime.now(UTC)})
        threading.Thread(target=self._job, args=(provider,), name=f"program-{name}", daemon=True).start()
        return started

    # ---- the job -------------------------------------------------------------------------

    def _job(self, provider: Program) -> None:
        try:
            if not provider.available():
                self._install(provider)
            self._sign_in(provider)
        except ProgramFailed as failed:
            logger.warning("%s program: %s (%s)", provider.name, failed.code, failed)
            self._update(provider.name, step="failed", failure=failed.code, detail=str(failed)[:400], sign_in=None)
        except Exception as error:  # one job's crash is its own failure, never the runner's, never a stuck step
            logger.exception("%s program job crashed", provider.name)
            self._update(provider.name, step="failed", failure="install_failed", detail=f"{type(error).__name__}: {error}"[:400], sign_in=None)

    def _install(self, provider: Program) -> None:
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
        self._update(provider.name, available=True, version=self.version(provider))

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

    def _sign_in(self, provider: Program) -> None:
        """Its own sign-in command, kept waiting (stdin open, never written: no code is relayed)
        until its status says signed in, `sign_in_seconds` at most; Claude Code opens this
        computer's browser, Codex prints a device code the web shows."""
        signed_in, account = self._status(provider)
        if signed_in:
            self._update(provider.name, step="ready", signed_in=True, account=account, sign_in=None)
            return
        deadline = datetime.now(UTC) + timedelta(seconds=self.sign_in_seconds)
        self._update(provider.name, step="signing_in", signed_in=False, sign_in=AgentSignIn(expires_at=deadline))
        login = subprocess.Popen([provider.executable(), *provider.login_arguments], env=self.environment, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **process_group_options())
        threading.Thread(target=self._read_code, args=(provider, login, deadline), daemon=True).start()
        try:
            while True:
                time.sleep(self.poll_seconds)
                signed_in, account = self._status(provider)
                if signed_in:
                    self._update(provider.name, step="ready", signed_in=True, account=account, sign_in=None)
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

    def _read_code(self, provider: Program, login: subprocess.Popen, deadline: datetime) -> None:
        """A device code the sign-in prints, onto its state while it still waits; its output is never
        logged (threat M4)."""
        assert login.stdout is not None
        for line in login.stdout:
            word = self.ANSI.sub("", line).strip()
            if self.CODE.match(word):
                self._update(provider.name, only_while="signing_in", sign_in=AgentSignIn(code=word, expires_at=deadline))

    # ---- states ----------------------------------------------------------------------------

    def _update(self, name: str, *, only_while: AgentProgramStep | None = None, **changes: object) -> None:
        """One job state change, under the lock, never running anything: a terminal state is always
        written; a change `only_while` a step is dropped once the job left it."""
        if unknown := changes.keys() - AgentProviderState.model_fields.keys():
            raise ValueError(f"not a program state field: {sorted(unknown)}")
        with self._lock:
            current = self._jobs[name]
            if only_while is not None and current.step != only_while:
                return
            self._jobs[name] = AgentProviderState.model_validate({**current.model_dump(), **changes, "changed_at": datetime.now(UTC)})

    def _status(self, provider: Program) -> tuple[bool | None, str]:
        return asyncio.run(provider.login_status(self.environment))

    def _base(self, provider: Program) -> AgentProviderState:
        installed = provider.available()
        return AgentProviderState(provider=provider.name, active=self.active(provider.name), available=installed,
                                  version=self.version(provider) if installed else "")

    def _now(self, provider: Program, last: AgentProviderState | None) -> AgentProviderState:
        """What `provider` is here now; `last`, a finished job's failure, while it is not signed in, and
        its `changed_at` (when the web's job last moved it) while it is still ready: the server tells
        the owner of a sign-in once, by that time."""
        base = self._base(provider)
        signed_in, account = self._status(provider) if base.available else (False, "")
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
