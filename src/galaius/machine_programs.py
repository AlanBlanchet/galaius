"""The agent programs (Claude Code, Codex) this computer's owner installs from the web
(`AgentProgramInstallRequest`): its vendor's own install script, fetched from the address the
provider declares (`AgentProvider.installers`, never one the server sends), run for this user with
a system-only environment, then the program's own sign-in, waited for until its status command
says it is signed in. A subscription's sign-in (`subscription_id`) runs the same way inside that
subscription's own folder (`Subscription`), never touching the default login: one job per program
and login at a time; what each stands at is `AgentProviderState`."""

import asyncio
import base64
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
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from galaius_core import (
    DEVICE_CODE,
    SIGN_IN_CODE,
    AgentProgramFailure,
    AgentProgramStep,
    AgentProvider,
    AgentProviderState,
    AgentSignIn,
    UsageWindow,
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from galaius.agents.providers import PROVIDERS, LoginStatus, ProgramInstaller
from galaius.agents.providers import AgentProvider as Program
from galaius.agents.subscriptions import Subscription, SubscriptionRefused
from galaius.processes import end_process_tree, process_group_options
from galaius.sign_in_browser import SignInHandoff

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
    #: Each program's default login's last usage reading here (the runner's `SubscriptionWatch.login_usage`).
    usage: Callable[[str], tuple[UsageWindow, ...]] = Field(default=lambda _name: ())
    #: How downloads reach the network (a test's own transport; None: the system's).
    transport: httpx.BaseTransport | None = None
    install_seconds: ClassVar[float] = 600
    sign_in_seconds: ClassVar[float] = 15 * 60
    poll_seconds: ClassVar[float] = 3
    #: How long a sign-in may stay without an address or code before its terminal command is offered.
    terminal_seconds: ClassVar[float] = 20
    script_bytes: ClassVar[int] = 2 * 1024 * 1024
    redirects: ClassVar[int] = 5
    CODE: ClassVar[re.Pattern[str]] = re.compile(DEVICE_CODE)
    PASTED: ClassVar[re.Pattern[str]] = re.compile(SIGN_IN_CODE)
    #: How a code comes sealed to a sign-in's key (`AgentSignIn.seal_key`): what WebCrypto's RSA-OAEP with SHA-256 makes.
    SEAL: ClassVar[padding.OAEP] = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    ANSI: ClassVar[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*m")
    #: The steps a job is under way in: asked again, it is not started twice (threat M6).
    RUNNING: ClassVar[frozenset[str]] = frozenset({"installing", "signing_in"})

    #: Each job by its program and login (a subscription's id; None: the default login).
    _jobs: dict[tuple[str, UUID | None], AgentProviderState] = PrivateAttr(default_factory=dict)
    #: Each sign-in under way, by the same key: the program a pasted code is handed to (`paste`), and its attempt's id and
    #: own private key the code comes sealed to (`AgentSignIn.seal_key`), never written anywhere.
    _logins: dict[tuple[str, UUID | None], tuple[subprocess.Popen, UUID, rsa.RSAPrivateKey]] = PrivateAttr(default_factory=dict)
    #: Each login's last status check that completed and when (monotonic), by program and environment (`_status`).
    _answered: dict[tuple[str, frozenset[tuple[str, str]]], tuple[float, LoginStatus]] = PrivateAttr(default_factory=dict)
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
            started = self._jobs[key] = now.model_copy(update={"step": "signing_in" if now.available else "installing", "expired": False, "changed_at": datetime.now(UTC)})
        threading.Thread(target=self._job, args=(provider, subscription), name=f"program-{name}", daemon=True).start()
        return started

    def signed_in(self, name: str, subscription_id: UUID | None) -> bool | None:
        """Whether `name`'s login (`subscription_id`'s; None: the default one) is signed in here now, as its
        state reads (`_status`); a subscription no longer here is not."""
        try:
            subscription = Subscription.of(subscription_id) if subscription_id is not None else None
        except SubscriptionRefused:
            return False
        return self._status(PROVIDERS[name], self._environment(subscription)).signed_in

    def paste(self, name: AgentProvider, subscription_id: UUID | None, attempt: UUID, sealed: str) -> AgentProviderState:
        """The code its vendor's page showed the person (`AgentSignIn.paste`), `sealed` in his browser to sign-in `attempt`'s
        own key, opened here and written once to that program of `name` waiting here (`subscription_id`'s; None: the default
        login), its input then closed: nothing else reaches it. Never kept or logged. Its state after. Refused
        (PermissionError) when that sign-in no longer waits for a code (ended, another attempt since), one was already
        handed to it, or what came is not a code sealed to it."""
        key = name, subscription_id
        with self._lock:
            job, (login, waiting, private) = self._jobs.get(key), self._logins.get(key, (None, None, None))
            heard = None if job is None or job.step != "signing_in" else job.sign_in
            if heard is None or not heard.paste or heard.attempt != attempt or waiting != attempt or login is None or private is None or login.poll() is not None:
                raise PermissionError(f"no {name} sign-in waits for this code here: start it again")
            if heard.pasted:
                raise PermissionError(f"a code was already handed to this {name} sign-in: start it again for another")
            try:
                code = private.decrypt(base64.b64decode(sealed, validate=True), self.SEAL).decode("utf-8").strip()
            except (ValueError, UnicodeError) as error:
                raise PermissionError(f"not a {name} sign-in code sealed to this sign-in") from error
            if not self.PASTED.fullmatch(code):  # one line, nothing a terminal would read as more input
                raise PermissionError(f"not a {name} sign-in code")
            self._jobs[key] = job = job.model_copy(update={"sign_in": heard.model_copy(update={"pasted": True}), "changed_at": datetime.now(UTC)})
            assert login.stdin is not None
            try:
                login.stdin.write(code + "\n")
                login.stdin.close()
            except OSError:  # it ended between the check and the write: its job ends failed, saying so
                pass
        return job

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
            self._answered.pop((subscription.program.name, frozenset(self._environment(subscription).items())), None)
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
        """Its own sign-in command, kept waiting (stdin open) until its status says signed in, `sign_in_seconds` at most.
        Claude Code prints an address any browser signs in at (`paste_link`): the web opens it in the person's browser,
        wherever he is, and the code its page then shows comes back once (`paste`); this PC opens no browser. Codex prints
        a device code the web shows. A program with neither: the address it handed `BROWSER` (`SignInHandoff`), to open
        on this PC; none after `terminal_seconds`: the command to run it by hand in a terminal here. A subscription's runs
        inside its folder, and is undone when the default login no longer names the same login after it."""
        key, environment = self._key(provider, subscription), self._environment(subscription)
        status = self._status(provider, environment)
        if status.signed_in:
            self._update(key, step="ready", signed_in=True, expired=False, account=status.account, login=status.login, sign_in=None)
            return
        default = self._status(provider, self.environment).login if subscription is not None else ""
        deadline = datetime.now(UTC) + timedelta(seconds=self.sign_in_seconds)
        self._update(key, step="signing_in", signed_in=False, expired=False, sign_in=AgentSignIn(expires_at=deadline))
        terminal = time.monotonic() + self.terminal_seconds
        # 0700 on POSIX; on Windows a helper still holding the file never hides how the sign-in ended.
        with tempfile.TemporaryDirectory(prefix="galaius-sign-in-", ignore_cleanup_errors=True) as folder:
            handoff = SignInHandoff(link_file=Path(folder) / "link", browser=environment.get("BROWSER", ""), opens=provider.paste_link is None)
            login = subprocess.Popen([provider.executable(), *provider.login_arguments], env={**environment, **handoff.environment()}, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **process_group_options())
            attempt, private = uuid4(), rsa.generate_private_key(public_exponent=65537, key_size=2048)
            with self._lock:
                self._logins[key] = login, attempt, private
            seal = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
            threading.Thread(target=self._read_output, args=(provider, key, login, attempt, seal), daemon=True).start()
            try:
                while True:
                    time.sleep(self.poll_seconds)
                    heard = self._jobs[key].sign_in
                    waited = time.monotonic() >= terminal
                    if heard is None or heard.paste or heard.code is not None:
                        pass
                    elif (link := handoff.link()) is not None and (provider.paste_link is None or waited):
                        self._signing(key, link=link)  # no address for any browser printed: this PC's own
                    elif waited and heard.link is None:
                        self._signing(key, command=provider.login_command(subscription.overlay() if subscription is not None else {}))
                    status = self._status(provider, environment)
                    if status.signed_in:
                        if subscription is not None and self._status(provider, self.environment).login != default:
                            self._logout(subscription)
                            raise ProgramFailed("sign_in_failed", "the sign-in changed this computer's default login; the subscription was signed out again")
                        self._update(key, step="ready", signed_in=True, account=status.account, login=status.login, sign_in=None)
                        return
                    if login.poll() is not None:
                        if (heard := self._jobs[key].sign_in) is not None and heard.pasted:
                            raise ProgramFailed("code_refused", f"its sign-in ended (exit {login.returncode}) after the code: its vendor refused it")
                        raise ProgramFailed("sign_in_failed", f"its sign-in ended (exit {login.returncode}) without signing in")
                    if datetime.now(UTC) >= deadline:
                        raise ProgramFailed("sign_in_expired", f"not signed in within {self.sign_in_seconds / 60:.0f} min")
            finally:
                with self._lock:
                    self._logins.pop(key, None)
                if login.poll() is None:
                    end_process_tree(login.pid)
                    try:
                        login.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        login.kill()

    def _read_output(self, provider: Program, key: tuple[str, UUID | None], login: subprocess.Popen, attempt: UUID, seal: str) -> None:
        """What the sign-in prints for the person, onto its state while it still waits: a device code, or the address any
        browser signs in at (`paste_link`, then waiting for the code its page shows). Its output is never logged (threat M4)."""
        assert login.stdout is not None
        for line in login.stdout:
            if (heard := self._jobs[key].sign_in) is not None and heard.pasted:
                continue  # after the code, nothing it prints reaches the web (it may echo it)
            word = self.ANSI.sub("", line).strip()
            if self.CODE.match(word):
                self._signing(key, code=word)
            elif provider.paste_link is not None and (found := provider.paste_link.search(word)) is not None:
                self._signing(key, link=found.group(1), paste=True, attempt=attempt, seal_key=seal, command=None)  # over a terminal command offered first

    def _signing(self, key: tuple[str, UUID | None], **found: object) -> None:
        """What the sign-in under way just said (its device code, the address it opened), onto its state
        while it still waits; what is not a sign-in's (an address that is not https) is dropped, unlogged."""
        with self._lock:
            current = self._jobs[key]
            if current.step != "signing_in" or current.sign_in is None:
                return
            try:
                heard = AgentSignIn.model_validate({**current.sign_in.model_dump(), **found})
            except ValidationError as refused:
                # Which rule it broke, never the value (an address, a code: threat M4).
                logger.info("%s sign-in: dropped what it reported (%s)", key[0], ", ".join(f"{error['loc'][0]}: {error['type']}" for error in refused.errors()))
                return
            if heard != current.sign_in:
                self._jobs[key] = current.model_copy(update={"sign_in": heard, "changed_at": datetime.now(UTC)})

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

    def _status(self, provider: Program, environment: dict[str, str]) -> LoginStatus:
        """Its login now; a check that could not complete (a CLI slow to answer on a busy PC) is its last
        completed answer within `LOGIN_REUSE_SECONDS`, never « signed out »; past that, unknown."""
        key = provider.name, frozenset(environment.items())
        status = asyncio.run(provider.login_status(environment))
        with self._lock:
            if status.signed_in is not None:
                self._answered[key] = time.monotonic(), status
            elif (last := self._answered.get(key)) is not None and time.monotonic() - last[0] < provider.LOGIN_REUSE_SECONDS:
                return last[1]
        return status

    def _logout(self, subscription: Subscription) -> None:
        """Its program's own sign-out inside its folder, then the folder. A sign-out that fails keeps
        the folder (refused, saying why): where the login also lives outside it (macOS keychain, keyed
        by the folder's path), removing the folder alone would orphan it."""
        if subscription.program.logout_arguments and subscription.program.available():
            try:
                done = subprocess.run([subscription.program.executable(), *subscription.program.logout_arguments], env=self._environment(subscription),
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=60)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise PermissionError(f"{subscription.provider}'s sign-out did not complete ({type(error).__name__}); the subscription stays, sign out again") from error
            if done.returncode != 0:
                raise PermissionError(f"{subscription.provider}'s sign-out failed: {self.last_line(done.stderr or done.stdout) or f'exit {done.returncode}'}; the subscription stays, sign out again")
        subscription.remove()

    def _base(self, provider: Program, subscription: Subscription | None = None) -> AgentProviderState:
        installed = provider.available()
        return AgentProviderState(provider=provider.name, active=self.active(provider.name), available=installed,
                                  version=self.version(provider) if installed else "",
                                  subscription_id=subscription.id if subscription is not None else None,
                                  usage=subscription.usage() if subscription is not None else self.usage(provider.name))

    def _now(self, provider: Program, last: AgentProviderState | None, subscription: Subscription | None = None) -> AgentProviderState:
        """What `provider` is here now; `last`, a finished job's failure, while it is not signed in, and
        its `changed_at` (when the web's job last moved it) while it is still ready: the server tells
        the owner of a sign-in once, by that time."""
        base = self._base(provider, subscription)
        status = self._status(provider, self._environment(subscription)) if base.available else LoginStatus(signed_in=None)
        if not status.signed_in and last is not None and last.step == "failed":
            return last
        step: AgentProgramStep = "missing" if not base.available else "ready" if status.signed_in else "signed_out"
        kept = last.changed_at if last is not None and last.step == step else None
        return base.model_copy(update={"step": step, "signed_in": status.signed_in, "expired": status.expired, "account": status.account, "login": status.login, "changed_at": kept,
                                       "usage": base.usage if step == "ready" else ()})

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
