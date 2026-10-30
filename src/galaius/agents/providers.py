"""Adapters for the agent CLIs galaius can supervise.

**The legal boundary lives here.** galaius builds an argv, spawns the vendor's own binary, and
reads its stdout. It never reads, stores, forwards or proxies a credential — the CLI
authenticates itself with the user's own login, on the user's own machine. Anthropic's terms
forbid third parties *routing requests through* Free/Pro/Max credentials, and an enforcement wave
in early 2026 hit tools that did; running the documented headless mode (`claude -p`, which
Anthropic markets for scripts and CI) is a different act entirely. Never add a "sign in with
Claude" flow, never read a credentials file, never expose an endpoint backed by a subscription.

Cross-provider work needs no bespoke protocol: every one of these CLIs is an MCP *client*, and
galaius is an MCP *server*. Handing a spawned agent galaius's own server config (``mcp_config``)
is what lets a Claude agent spawn a Codex agent — they meet on MCP, which is vendor-neutral.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Annotated, ClassVar, Literal, Protocol, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from galaius.caller import Caller
from galaius_core import AgentMedia, ContextGauge, LoginIdentity, UsageWindow, UsageWindowName

from galaius import installed_version
from galaius.agents.catalog import AgentCatalog
from galaius.agents.events import TOKEN_FIELDS, AgentEvent, CompactionTrigger, RunCut, TokenUsage, UsageLedger, instant, stream_lines
from galaius.agents.quota import REFUSAL
from galaius.agents.profiles import overlay_for
from galaius.agents.vocabulary import ApprovalIntent, TouchScope, ThinkingLevel, vocabulary_for
from galaius.brief_hook import NOTE as BRIEF_NOTE
from galaius.fence import COMPAT_KEY, KEYS, RELAY, RELAY_PORT, Confinement, RelayedServer, SignIn, confinement_refusal, userns_refusal
from galaius.inbox_hook import NOTE as INBOX_NOTE
from galaius.models import Model
from galaius.processes import ask_process, run_isolated_process
from galaius.mcp_announcement import GatewayServed

#: Set (to anything) in the environment of an agent working in a folder whose own agent settings
#: must not load - a workspace cloned from the web: its hooks and permission rules are code and
#: policy its owner never read. Children inherit it, so a subagent there obeys it too.
PROJECT_SETTINGS_OFF = "GALAIUS_PROJECT_SETTINGS_OFF"
#: The folders put on this PC from the web (clones, copies), joined by `os.pathsep`: code its owner never read,
#: where galaius runs no git of its own (`run_folder.restore`). Set by the PC runner for every child it starts.
UNTRUSTED_FOLDERS = "GALAIUS_UNTRUSTED_FOLDERS"

#: The most text one command-line argument carries: Linux refuses an argument past 131,072 bytes (E2BIG; macOS's whole
#: line is far larger), Windows a whole command line past 32,767 characters — room left for the CLI's own options. A
#: turn's text past it goes on the child's stdin (`AgentProvider.on_stdin`, `task_message`).
ARGV_TEXT_BYTES = 16_000 if sys.platform == "win32" else 100_000


def fits_argv(text: str) -> bool:
    """Whether `text` can be one command-line argument here (`ARGV_TEXT_BYTES`; Windows counts UTF-16 units, a byte
    count bounds them)."""
    return len(text.encode()) <= ARGV_TEXT_BYTES


#: A transcript is read by a human, and a 50k-char tool result is not read — it is scrolled past,
#: while bloating every refresh that parses the file. Keep the head, say what was cut.
_CLIP = 2000
def _safe_process_detail(value: str) -> str:
    """One redacted diagnostic line; child output must never become a credential log."""
    value = re.sub(
        r"(?i)(api[_-]?key|auth[_-]?token|authorization|bearer)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        value,
    )
    value = re.sub(
        r"(?:[A-Za-z]:)?[/\\][^\s:]*media-jobs[/\\]job-[^\s:/\\]+(?:[/\\][^\s:]*)?",
        "[media-stage]",
        value,
    )
    return _clip(value.replace("\x00", ""), 500)


def _powershell_quoted(text: str) -> str:
    """`text` as one PowerShell literal."""
    return "'" + text.replace("'", "''") + "'"


def _clip(text: str, limit: int = _CLIP, tail: int = 0) -> str:
    """At most `limit` characters kept: the opening, and the last `tail` of them when the end is
    what the text concludes with (a Stop-hook block closes on its verdict)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit - tail
    return f"{text[:head]}… (+{len(text) - limit} chars) …{text[len(text) - tail:]}" if tail else f"{text[:limit]}… (+{len(text) - limit} chars)"


def _summarise_input(value) -> str:
    """The tool's arguments as one readable line. A dict is rendered key=value with its most
    telling field first — the command, the path, the pattern — because that is what identifies
    the call at a glance."""
    if value is None:
        return ""
    if not isinstance(value, dict):
        return _clip(str(value), 300)
    lead = ("command", "file_path", "path", "pattern", "url", "query", "prompt")
    keys = [k for k in lead if k in value] + [k for k in value if k not in lead]
    return _clip(" ".join(f"{k}={value[k]!r}" for k in keys[:4]), 300)


@dataclass(frozen=True)
class PermissionMode:
    """One answer to "how much may this agent do on its own?".

    Supervising a team is largely this decision, made per member: the researcher may read, the
    one refactoring may write, the one you have not watched yet plans and touches nothing. It is
    a per-run choice rather than a global setting because a team is heterogeneous by design.
    """

    id: str
    label: str
    detail: str
    #: True for a mode that acts without asking. Offered — refusing to expose it only pushes
    #: people to a terminal where the choice is invisible to the panel — but never rendered as an
    #: unremarkable option beside the others.
    unrestricted: bool = False
    #: Provider spelling for this shared intent. Never compare this value across providers.
    provider_mode: str | None = None
    #: Provider-independent axes. Native modes may combine these axes; adapters must preserve
    #: the caller's intent or safely narrow it, never infer it from a vendor spelling.
    touch: TouchScope | None = None
    approval: ApprovalIntent | None = None


@dataclass(frozen=True)
class _MediaResult:
    text: str
    input_tokens: int
    output_tokens: int
    reported_cost_usd: float | None


@dataclass
class _MediaProcessFailure(Exception):
    message: str
    events: tuple[AgentEvent, ...] = ()
    exit_code: int | None = None
    stderr_bytes: int | None = None
    stderr_sha256: str | None = None
    timeout_phase: str | None = None
    elapsed_seconds: float | None = None

    def __str__(self) -> str:
        return self.message


class _MediaProcessTimeout(_MediaProcessFailure, TimeoutError):
    pass


def _failure_event_facts(events: list[AgentEvent]) -> tuple[AgentEvent, ...]:
    """Retain accounting/session facts without retaining provider text or tool payloads."""
    return tuple(
        AgentEvent(
            kind="other",
            session_id=event.session_id,
            cost_usd=event.cost_usd,
            **{field: getattr(event, field) for field in TOKEN_FIELDS},
        )
        for event in events
        if event.session_id is not None
        or event.cost_usd is not None
        or any(getattr(event, field) is not None for field in TOKEN_FIELDS)
    )


class UnsupportedToolPolicy(ValueError):
    """The adapter cannot expose the requested role tool set without weakening it."""


def no_tools(allowed_tools: list[str] | None) -> bool:
    """A role restricted to no tool at all (`AgentRevision.harness_tools` ()), as opposed to an unrestricted one (None)."""
    return allowed_tools == []


DeniedTool = Annotated[str, StringConstraints(
    pattern=r"^(?:[A-Z][A-Za-z0-9]*|mcp__[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+)$",
    max_length=256,
)]
_DENIED_TOOLS = TypeAdapter(tuple[DeniedTool, ...])


def validate_denied_tools(tools: tuple[str, ...]) -> None:
    try:
        _DENIED_TOOLS.validate_python(tools)
    except ValueError as exc:
        raise ValueError("Denied tool must be an exact built-in or qualified MCP tool name") from exc


class ProgramInstaller(BaseModel):
    """A vendor's own install script for one kind of system (`AgentProvider.installers`), run for
    this computer's user only, never as an administrator: fetched over https from `url`, through the
    hosts in `hosts` alone, handed to `shell` with `environment` added; the program then lands in one
    of `bin_dirs` (`~` and `%VARIABLE%` expanded), which the installer does not always put on PATH."""

    model_config = ConfigDict(frozen=True)

    url: str
    hosts: frozenset[str]
    shell: tuple[str, ...]
    bin_dirs: tuple[str, ...]
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def from_its_vendor(self) -> "ProgramInstaller":
        parts = urlsplit(self.url)
        if parts.scheme != "https" or parts.hostname not in self.hosts:
            raise ValueError(f"an installer is an https address on its vendor's hosts, not {self.url}")
        return self

    def found_dirs(self) -> tuple[str, ...]:
        expanded = (os.path.expandvars(os.path.expanduser(entry)) for entry in self.bin_dirs)
        return tuple(entry for entry in expanded if "%" not in entry and "$" not in entry)


#: How PowerShell runs a downloaded script once, whatever the user's script policy (no profile, no prompt).
POWERSHELL_FILE = ("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File")
#: Where each vendor serves its installers (each redirects to its download host).
ANTHROPIC_INSTALL_HOSTS = frozenset({"claude.ai", "downloads.claude.ai"})
OPENAI_INSTALL_HOSTS = frozenset({"chatgpt.com", "releases.openai.com"})


class LoginStatus(LoginIdentity):
    """What a CLI's own status commands say of one login, asked now (`AgentProvider.login_status`): as whom
    (`signed_in_as`) and which login (a digest of `login_identity`), once signed in."""

    #: Signed in; None: its status check could not complete.
    signed_in: bool | None
    #: Signed out because the login saved here expired and could not be refreshed (`expiry_command`).
    expired: bool = False


#: A pay-per-use model API key by its variable's name, `<PROVIDER>_API_KEY` (`OPENAI_API_KEY`,
#: `GEMINI_API_KEY`: the naming `UserConfig` and `list_providers` read keys by; a tool's own token
#: under another name stays): an agent never holds one but its own CLI's login key
#: (`AgentProvider.child_environment`) — a model or image call it needs goes through the Galaius
#: platform, with the company's connection, charged and counted (owner 2026-10-14: « credits are being
#: spent … by using gpt image 2 … But they are not referenced »).
MODEL_API_KEY = re.compile(r"[A-Za-z0-9_]+_API_KEY")


class AgentProvider(ABC):
    """How to launch one vendor's agent CLI and read what it emits."""

    name: ClassVar[str]
    binary: ClassVar[str]
    #: Can a finished/running session be CONTINUED with a new message? This is what makes
    #: agent-to-agent messaging possible without inventing a mailbox: the recipient keeps its
    #: own context instead of being handed a cold summary of it.
    can_resume: ClassVar[bool] = False
    can_queue: ClassVar[bool] = False
    #: False when the flags below were written from documentation but never exercised against a
    #: real binary — the adapter says so instead of pretending to be tested.
    verified: ClassVar[bool] = True
    caveat: ClassVar[str | None] = None
    #: The variables naming the folder this CLI keeps its login, settings and sessions in (the first
    #: one decides), and that folder's name under the user's home when none is set (`auth_home`).
    auth_home_env: ClassVar[tuple[str, ...]] = ()
    home_folder: ClassVar[str] = ""
    #: Its user settings file in that folder, when it has one a subscription folder takes keys of
    #: (`Subscription.adopt`).
    settings_file: ClassVar[str | None] = None
    permission_option: ClassVar[str] = "--permission-mode"
    #: How this CLI is told to load none of the folder's own settings (`PROJECT_SETTINGS_OFF`);
    #: None: it cannot be, so it never starts or resumes where they must not load.
    folder_settings_off: ClassVar[tuple[str, ...] | None] = None
    #: Whether a turn of this CLI reads a message sent while it works after its current tool call
    #: (`ClaudeTurnSettings.inbox_hook`); otherwise the message waits for the turn's end.
    reads_mid_turn: ClassVar[bool] = False
    #: Whether its `command` and `resume_command` take `turn` (`ClaudeTurnSettings`): what galaius sets in each agent turn.
    takes_turn_settings: ClassVar[bool] = False
    #: Seconds a confirmed login is believed before `authenticated` asks the CLI again: its status
    #: command is a CLI start of its own (`claude auth status`: 0.6 s), paid by every start before.
    #: A login lost within it fails that run at its start instead of passing to the next candidate.
    LOGIN_REUSE_SECONDS: ClassVar[float] = 300.0
    #: Each confirmed login: when, and which (`login`).
    _logged_in: ClassVar[dict[tuple[str, str], tuple[float, str]]] = {}
    #: Whether a child can start before its task is known and read it as its first message
    #: (`ahead_command`, `task_message`, `startup_files`): its own startup is then paid while
    #: nobody waits (`galaius.agents.warm.WarmStart`).
    starts_ahead: ClassVar[bool] = False
    #: The turn that makes this CLI compact its conversation (replace it with its own summary), sent
    #: as a resumed turn's whole prompt; None: it has none, a compaction is refused (`compact_turn`).
    compact_prompt: ClassVar[str | None] = None

    def session_files(self, home: Path, cwd: Path, session: str) -> tuple[Path, ...]:
        """The files holding session `session` (started in `cwd`) inside login folder `home`, the ones its resume reads:
        what continuing it under another login folder carries over (contract A9); empty for a CLI that cannot resume."""
        return ()

    def ahead_command(self, **arguments) -> list[str]:
        """`command`'s child with no task: it starts now and reads the task `task_message` writes."""
        raise UnsupportedToolPolicy(f"{self.name} cannot start before its task")

    def task_message(self, task: str, images: tuple[Path, ...] = ()) -> bytes:
        """`task`, and the images at `images` (`stdin_images`), as the first message an `ahead_command` child reads on its stdin."""
        raise UnsupportedToolPolicy(f"{self.name} cannot start before its task")

    def on_stdin(self, argv: list[str]) -> tuple[list[str], bytes | None]:
        """`argv`, a command ending with its turn's text, as it starts whatever that text's length: unchanged when the
        text fits one argument (`fits_argv`), else with `stdin_prompt` in its place and the text for its stdin."""
        if fits_argv(argv[-1]):
            return argv, None
        if self.stdin_prompt is None:
            raise UnsupportedToolPolicy(f"{self.name} reads no turn on its stdin, and {len(argv[-1].encode()):,} bytes do not fit its command line")
        return [*argv[:-1], self.stdin_prompt], argv[-1].encode()

    def startup_files(self, cwd: Path, environment: Mapping[str, str]) -> tuple[Path, ...]:
        """The files this CLI reads once, when it starts in `cwd`: a child started ahead is used only
        while none of them changed since."""
        return ()

    def user_instructions(self, environment: Mapping[str, str]) -> Path | None:
        """The user-level instructions file this CLI loads at every start in `environment`."""
        return None

    def _setting_sources(self, environment: Mapping[str, str] | None = None) -> list[str]:
        """`folder_settings_off` when the folder's own settings must not load (`PROJECT_SETTINGS_OFF`
        in `environment`, the one the CLI runs in; this process's own when None), else nothing."""
        if not Caller.environ(environment).get(PROJECT_SETTINGS_OFF):
            return []
        if self.folder_settings_off is None:
            raise UnsupportedToolPolicy(f"{self.name} cannot run in a workspace cloned from the web: its folder's own settings cannot be switched off")
        return list(self.folder_settings_off)
    #: Provider advertises whether its initial-prompt CLI has a native image attachment flag.
    #: The concrete provider still verifies the installed binary before using it.
    can_attach_images: ClassVar[bool] = False
    #: Whether the message a child started ahead reads on its stdin (`task_message`) carries images itself: a turn
    #: started that way is handed them whole, never their paths.
    stdin_images: ClassVar[bool] = False
    #: What stands for the turn's text, its command's last argument, when this CLI is to read that text from its stdin
    #: (`on_stdin`); None: it reads none there.
    stdin_prompt: ClassVar[str | None] = None

    #: Its vendor's install script, per kind of system (`windows`, `posix`); none: galaius does not install it.
    installers: ClassVar[dict[Literal["windows", "posix"], ProgramInstaller]] = {}
    #: Its own sign-in and sign-out commands, run with no terminal (`AgentPrograms`).
    login_arguments: ClassVar[tuple[str, ...]] = ()
    logout_arguments: ClassVar[tuple[str, ...]] = ()
    #: The line of its sign-in's output naming (group 1) an address any browser signs in at, whose page then shows a code
    #: the sign-in waits for on its input (`AgentSignIn.paste`); none: its sign-in returns to this PC by itself.
    paste_link: ClassVar[re.Pattern[str] | None] = None
    #: What a whole code its page shows looks like: anything else is refused before it reaches the program (which may keep
    #: waiting on a code it cannot use).
    paste_code: ClassVar[re.Pattern[str] | None] = None

    def login_keys(self, environment: Mapping[str, str]) -> frozenset[str]:
        """The key variables this CLI's own login reads in `environment` (`fence.KEYS`)."""
        return frozenset(KEYS.get(self.name, ()))

    #: The variable making this CLI's turn wait (milliseconds) for its MCP servers to connect before it starts; none: it
    #: waits by itself or not at all.
    mcp_wait_variable: ClassVar[str | None] = None

    def child_environment(self, environment: Mapping[str, str], *, mcp_wait_seconds: float) -> dict[str, str]:
        """`environment` as this CLI's agent runs with it: every model API key gone but its own login's
        (`MODEL_API_KEY`), and its turn waiting up to `mcp_wait_seconds` for its MCP servers, galaius's tools among them
        (`mcp_wait_variable`; a value the caller set stands). galaius's own commands it runs read their keys from this
        computer's settings."""
        own = self.login_keys(environment)
        kept = {name: value for name, value in environment.items() if name in own or not MODEL_API_KEY.fullmatch(name)}
        if self.mcp_wait_variable is not None and mcp_wait_seconds > 0:
            kept.setdefault(self.mcp_wait_variable, str(round(mcp_wait_seconds * 1000)))
        return kept

    def auth_home(self, environment: Mapping[str, str]) -> Path:
        """The folder this CLI keeps its login in, in `environment`: its `auth_home_env` value
        (a subscription's own folder), else `home_folder` under the user's home."""
        named = next((environment[name] for name in self.auth_home_env if environment.get(name)), None)
        return Path(named) if named else Path(environment.get("HOME") or Path.home()) / self.home_folder

    def usage(self, line: dict) -> tuple[UsageWindow, ...]:
        """The usage windows one stream line reports (`AgentEvent.usage`); none for a CLI that reports none."""
        return ()

    def usage_command(self) -> tuple[list[str], tuple[dict, ...]] | None:
        """How this CLI says its own login's usage now (every session of that login counted, the owner's own editor
        sessions too), asked of its vendor with that login by the CLI itself: galaius never reads the credential.
        The command and the lines it is sent; None: it has no such way."""
        return None

    def usage_answer(self, line: dict) -> tuple[UsageWindow, ...] | None:
        """The usage an output line of `usage_command` answers (no window: none, the login signed out or metered by no
        plan); None: not its answer."""
        return None

    #: The keys of the answer naming its account (its JSON status, `account_command`'s) that say which account it is
    #: (`identity_of`): never where the login is kept nor its plan (an upgrade is the same account).
    IDENTITY_KEYS: ClassVar[tuple[str, ...]] = ()

    def identity_of(self, said: object) -> str:
        """What names the account in `said` (`IDENTITY_KEYS`); "" when it is no object."""
        return json.dumps({key: said[key] for key in self.IDENTITY_KEYS if key in said}, sort_keys=True) if isinstance(said, dict) else ""

    def account_command(self) -> tuple[list[str], tuple[dict, ...]] | None:
        """How this CLI names the account its login is when its status command does not (`login_status`): the command
        and the lines it is sent; None: its status answer names it (`login_identity`)."""
        return None

    def account_answer(self, line: dict) -> tuple[str, str] | None:
        """Who an output line of `account_command` says the login is signed in as, and what names that account; ("", ""):
        it names none (a CLI too old for the request), the status answer's then; None: not its answer."""
        return None

    async def current_usage(self, env: dict[str, str], *, timeout: float = 30) -> tuple[UsageWindow, ...] | None:
        """The usage of the login `env` names now, as its CLI says (`usage_command`); None: it could not be asked (no
        such command, not installed, no answer in time): its last reading stands."""
        asked = self.usage_command() if self.available() else None
        if asked is None:
            return None
        return await ask_process(asked[0], cwd=Path(env.get("HOME") or Path.home()), env=env, lines=asked[1], answer=self.usage_answer, timeout=timeout)

    @classmethod
    def installer(cls) -> ProgramInstaller | None:
        """Its install script for this system, None where its vendor publishes none."""
        return cls.installers.get("windows" if sys.platform == "win32" else "posix")

    @staticmethod
    def search_path(path: str | None = None) -> str:
        """`path` (this process's PATH by default) followed by every program's own user folder here."""
        current = (os.environ.get("PATH", os.defpath) if path is None else path).split(os.pathsep)
        installed = (entry for provider in PROVIDERS.values() if (installer := provider.installer()) is not None for entry in installer.found_dirs())
        return os.pathsep.join(dict.fromkeys(entry for entry in (*current, *installed) if entry))  # an empty entry means the current folder

    def located(self) -> str | None:
        """Where the CLI is found on the search path, as found (never the versioned file a link there
        points to, gone after its next update); None when it is not installed."""
        return shutil.which(self.binary, path=self.search_path())

    def available(self) -> bool:
        """Is the CLI installed? (Being logged in is the CLI's business, never ours.)"""
        return self.located() is not None

    def executable(self) -> str:
        """Resolve the installed executable for a CLI status probe."""
        found = self.located()
        if found is None:
            raise FileNotFoundError(f"{self.name} CLI is not installed")
        return str(Path(found).resolve())

    def login_command(self, overlay: Mapping[str, str]) -> str:
        """Its sign-in as typed in a terminal here (PowerShell on Windows), with `overlay` (a
        subscription's folder) set for it."""
        arguments = [self.located() or self.binary, *self.login_arguments]
        if sys.platform == "win32":
            return "".join(f"$env:{name}={_powershell_quoted(value)}; " for name, value in overlay.items()) + "& " + " ".join(map(_powershell_quoted, arguments))
        return " ".join([*(f"{name}={shlex.quote(value)}" for name, value in overlay.items()), shlex.join(arguments)])

    def signed_in_as(self, stdout: str, stderr: str) -> str:
        """Who its status command says it is signed in as (an e-mail, an organisation); "" when it does not say."""
        return ""

    def subscription_env(
        self, base: dict[str, str] | None = None, *, temp_dir: Path | None = None
    ) -> dict[str, str]:
        """Minimal environment for a subscription-authenticated child.

        API keys, auth-token overrides and base URLs are absent — an existing API key can't
        silently take precedence over the user's Claude.ai/ChatGPT subscription, and unrelated
        credentials stay out of child tools and diagnostics.
        """
        source = Caller.environ(base)
        exact = {
            "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            # The user bus: `systemd-run --user` (the agent ceiling, `contained`) cannot start the
            # child without it ("Failed to connect to bus"). Session plumbing, never a credential.
            "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
            # Windows' own plumbing: without SYSTEMROOT a child cannot open a socket (WinError
            # 10106); USERPROFILE / APPDATA are where the CLI finds its login; TEMP where it writes.
            "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "USERPROFILE", "HOMEDRIVE",
            "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "PROGRAMW6432", "COMMONPROGRAMFILES", "TEMP", "TMP", "USERNAME", "COMPUTERNAME",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
        }
        exact.update(self.auth_home_env)
        env = {
            key: value
            for key, value in source.items()
            if key in exact or key.startswith("LC_")
        }
        if temp_dir is not None:
            env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(temp_dir)
        return env

    def auth_command(self) -> list[str]:
        """Non-interactive command reporting how this CLI is authenticated."""
        raise NotImplementedError

    def accepts_subscription_auth(self, stdout: str, stderr: str) -> bool:
        """True only for the vendor's consumer-subscription login, never API-backed auth."""
        raise NotImplementedError

    def accepts_any_auth(self, stdout: str, stderr: str) -> bool:
        """True for ANY login the CLI reports — subscription or key. The launch pool asks this,
        never the subscription-only test: an agent child inherits the caller's environment, so a
        key-authenticated CLI runs it as well as a subscription one does."""
        return self.accepts_subscription_auth(stdout, stderr)

    #: The cut each HTTP status of its provider's API names when a turn fails on it (`cut_of`): 401, the login
    #: was refused (expired, revoked, switched to another account). A 429 is no cut by itself: only a refusal naming its
    #: window (`quota.REFUSAL`) is a usage limit, a short rate limit is the turn's own failure.
    CUT_STATUS: ClassVar[Mapping[int, RunCut]] = {401: RunCut.login_lost}
    #: What a 401 says when an API key was refused, not a login (`cut_of`): its words decide then (« Invalid API key ·
    #: Please run /login » is a login to sign in again).
    KEY_REFUSED: ClassVar[re.Pattern[str]] = re.compile(r"\bAPI[ _-]?key\b", re.IGNORECASE)
    #: What a turn says when its program never reached its provider's API — provider-neutral by construction: the CLI's
    #: own sentence (Claude Code 2.1.288, recorded verbatim 2026-10-22/23: « API Error: Can't reach the
    #: API server — check your internet or DNS (ENOTFOUND) »), else the operating system's own name for the failure, which
    #: every one of these CLIs passes through from its HTTP library. No word here is an answer about the TURN: the vendor
    #: never saw it, so the run is cut, never failed (`RunCut.network_lost`). A timeout is left out on purpose — a request
    #: that was answered slowly, or not at all, may have reached the model.
    NETWORK_LOST: ClassVar[re.Pattern[str]] = re.compile(
        r"can'?t reach the API server|could not (?:reach|connect to) the API"
        r"|\b(?:ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|EHOSTUNREACH|ENETUNREACH|ENETDOWN)\b"
        r"|getaddrinfo|\bdns (?:failure|lookup failed)\b|\bfetch failed\b",
        re.IGNORECASE)
    #: The words a failed turn says each cut in when its program gives no status (`cut_of`), checked in order: a usage
    #: refusal first — one is answered by the vendor's own server, so it reached it.
    cut_words: ClassVar[Mapping[RunCut, re.Pattern[str]]] = {RunCut.usage_limit: REFUSAL, RunCut.network_lost: NETWORK_LOST}

    @classmethod
    def cut_of(cls, text: str, status: object = None) -> RunCut | None:
        """What cut a turn that failed saying `text`, its API having answered `status` when its program says
        it (`RunCut`); None: its own failure."""
        if isinstance(status, int) and (cut := cls.CUT_STATUS.get(status)) is not None \
                and not (cut is RunCut.login_lost and cls.KEY_REFUSED.search(text)):
            return cut
        return next((cut for cut, words in cls.cut_words.items() if words.search(text)), None)

    #: What `expiry_command` says of a login `auth_command` still reports signed in but that expired and
    #: could not be refreshed; None: its status command alone says.
    expired_said: ClassVar[re.Pattern[str] | None] = None

    def expiry_command(self) -> list[str] | None:
        """A second status command, asked once `auth_command` reports signed in, that says `expired_said`
        while that login expired; None: its status command alone says."""
        return None

    async def _expired(self, env: dict[str, str], *, timeout: float) -> bool:
        """The login `auth_command` reports signed in expired: `expiry_command` failed AND said so
        (`expired_said`); a check that could not complete, or failed for another reason (a CLI too old
        for that command), says nothing."""
        argv = self.expiry_command()
        status = None if argv is None or self.expired_said is None else await self._auth_status(env, timeout=timeout, argv=argv)
        return status is not None and status[0] != 0 and self.expired_said.search(f"{status[1]}\n{status[2]}") is not None

    async def _auth_status(self, env: dict[str, str], *, timeout: float, argv: list[str] | None = None) -> tuple[int, str, str] | None:
        """Run the CLI's own auth-status command (`argv`: another of its status commands) without reading a credential store."""
        argv = self.auth_command() if argv is None else argv
        try:
            returncode, stdout_bytes, stderr_bytes = await run_isolated_process(
                argv, cwd=Path.cwd(), env=env, timeout=timeout
            )
        except (OSError, TimeoutError):
            return None
        return returncode, stdout_bytes.decode(errors="replace"), stderr_bytes.decode(errors="replace")

    async def subscription_authenticated(
        self, env: dict[str, str], *, timeout: float = 10
    ) -> bool:
        status = await self._auth_status(env, timeout=timeout)
        return status is not None and status[0] == 0 and self.accepts_subscription_auth(*status[1:]) and not await self._expired(env, timeout=timeout)

    async def login_status(self, env: dict[str, str], *, timeout: float = 10, expiry: bool = True, named: bool = True) -> LoginStatus:
        """Signed in, as whom and which login, asked now (`LoginStatus`): the login is a digest of the
        account its status answer names (`login_identity`), kept instead of the answer. It tells apart
        what the answer names — Claude Code: account and organisation; Codex: a ChatGPT login from an API
        key, never one ChatGPT account from another. A login its status still reports but `expiry_command`
        says expired is signed out (`LoginStatus.expired`); that second command runs only then, one CLI at a
        time, and never with `expiry` False (only who it is matters: an expired login is the same account). A CLI whose
        status names no account is asked who it is (`account_command`) unless `named` is False (a launch's check: whether,
        never who); that ask unanswered leaves the whole answer unknown."""
        status = await self._auth_status(env, timeout=timeout)
        if status is None:
            return LoginStatus(signed_in=None)
        if not (status[0] == 0 and self.accepts_any_auth(*status[1:])):
            return LoginStatus(signed_in=False)
        if expiry and await self._expired(env, timeout=timeout):
            return LoginStatus(signed_in=False, expired=True)
        said, identity = self.signed_in_as(*status[1:]), self.login_identity(*status[1:])
        if named and (asked := self.account_command()) is not None:
            answered = await ask_process(asked[0], cwd=Path(env.get("HOME") or Path.home()), env=env, lines=asked[1], answer=self.account_answer, timeout=timeout)
            if answered is None:  # never the status answer's instead: the same login would read as another one
                return LoginStatus(signed_in=None)
            said, identity = (answered[0] or said, answered[1]) if answered[1] else (said, identity)
        return LoginStatus(signed_in=True, account=said[:200], login=hashlib.sha256(identity.encode()).hexdigest()[:16])

    def login_identity(self, stdout: str, stderr: str) -> str:
        """The part of the status answer that names the login's account (`login_status`): its stdout, never a
        notice a CLI prints beside it on stderr (an update, a warning), which would make every check read as
        a new login. A CLI whose answer also says where the login is kept (its folder) names only the
        account, so one account signed in in two folders is one login."""
        return stdout

    def _login_key(self, env: dict[str, str]) -> tuple[str, str]:
        return self.name, hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()

    async def authenticated(self, env: dict[str, str], *, timeout: float = 10) -> bool | None:
        """CLI login state; None means its status check could not complete. A login confirmed less
        than `LOGIN_REUSE_SECONDS` ago in this process, for the same environment, is not checked
        again; a missing or unanswered one always is."""
        key = self._login_key(env)
        confirmed = self._logged_in.get(key)
        if confirmed is not None and time.monotonic() - confirmed[0] < self.LOGIN_REUSE_SECONDS:
            return True
        status = await self.login_status(env, timeout=timeout, named=False)
        if status.signed_in:
            self._logged_in[key] = (time.monotonic(), status.login)
        else:
            self._logged_in.pop(key, None)
        return status.signed_in

    def login(self, env: dict[str, str]) -> str:
        """Which login `authenticated` last confirmed for `env` (`login_status`); "" when none was (a
        provider without a status command)."""
        confirmed = self._logged_in.get(self._login_key(env))
        return "" if confirmed is None else confirmed[1]

    #: Catalog providers whose models this CLI runs through its OWN login — no API key in our env.
    native_providers: frozenset[str] = frozenset()

    def can_run(self, model: Model, env: dict[str, str]) -> bool:
        """Whether this CLI can be pointed at `model` at all — the pool a criterion chooses from.
        A criterion once picked the cheapest VLM in the whole catalog, a Gemini id, and handed it
        to the claude binary. Native = yes; anything else = no, unless a subclass knows a route."""
        return model.provider in self.native_providers

    def model_id_for(self, model: Model) -> str:
        """What a resolved criterion hands on: a bare id for a native model, `provider/id` for one
        that must be routed, so `resolve_model`'s overlay fires."""
        return model.id if model.provider in self.native_providers else f"{model.provider}/{model.id}"

    @staticmethod
    def validate_agent_name(agent: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", agent):
            raise ValueError("Agent name must start with a lowercase letter and contain only letters, digits, underscores or hyphens (80 characters maximum)")

    def definition_prompt(self, agent: str) -> str:
        self.validate_agent_name(agent)
        path = self.definition_path(agent)
        if path is None:
            raise UnsupportedToolPolicy(f"No prompt definition for role {agent!r}")
        text = path.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            frontmatter = re.match(r"---\n.*?\n---(?:\n|$)", text, re.S)
            if frontmatter is None:
                raise ValueError(f"Unterminated frontmatter for role {agent!r}")
            text = text[frontmatter.end():].strip()
        return text

    def validate_tool_policy(self, allowed_tools: list[str] | None, denied_tools: tuple[str, ...], *,
                             coarse_accepted: bool = False) -> None:
        """Refuse a role restriction (`allowed_tools` a list, empty included: no tool at all) this CLI cannot express, UNLESS the caller already recorded
        an explicit acceptance of coarser (sandbox-scope, not per-tool) enforcement instead — see
        :meth:`galaius.agents.policy.Policy.accepts_coarse_tool_policy`. A provider that genuinely
        cannot express ANY part of the restriction, coarse or not, ignores ``coarse_accepted``;
        only a provider whose own docstring says otherwise (:class:`CodexProvider`) honours it."""
        validate_denied_tools(denied_tools)
        if allowed_tools is not None or denied_tools:
            raise UnsupportedToolPolicy(f"{self.name} cannot enforce this role's tool restriction")

    @abstractmethod
    def command(self, task: str, *, cwd: str, model: str | None, mcp_config: str | None,
                run_id: str, agent: str | None = None,
                permission_mode: str | None = None,
                allowed_tools: list[str] | None = None,
                reasoning: str | None = None,
                agent_prompt: str | None = None, denied_tools: tuple[str, ...] = (),
                image_paths: tuple[Path, ...] = (), coarse_accepted: bool = False,
                base_url: str | None = None, environment: Mapping[str, str] | None = None) -> list[str]:
        """The argv to spawn for this task. ``agent`` names a definition the CLI resolves itself
        (Claude Code reads ~/.claude/agents/<name>.md), so a run can BE 'visual-critic'.
        ``coarse_accepted`` is the operator's recorded acceptance of sandbox-only enforcement for
        this role on this provider — see :meth:`validate_tool_policy`. ``base_url`` is the
        OpenAI-wire endpoint `resolve_model` routed this run to (HF's router, a self-hosted box);
        a provider that speaks its own vendor's endpoint natively via env (Claude Code) ignores it."""

    def permission_modes(self) -> list[PermissionMode]:
        """How much autonomy this CLI can be told to grant, or empty when we have not VERIFIED
        its flag against a real binary.

        Empty is the honest default. A guessed flag either fails the spawn or — the bad case —
        is accepted with a meaning we assumed, so the agent runs with permissions nobody chose.
        """
        return []

    def provider_permission_mode(self, intent: str | None) -> str | None:
        """Translate one shared permission intent into this CLI's flag vocabulary."""
        if intent is None:
            return None
        try:
            touch = TouchScope(intent)
        except ValueError:
            touch = None
        if touch is not None:
            return vocabulary_for(self.name).translate("touch", touch).native
        for mode in self.permission_modes():
            if mode.id == intent:
                # Permission modes are a compatibility surface for old callers. New provider
                # mappings come from the standard axes: Codex exposes touch and approval
                # separately; Claude combines them into one native permission mode.
                if mode.touch is not None:
                    vocabulary = vocabulary_for(self.name)
                    if self.name == "codex" or mode.touch in (TouchScope.READ_ONLY, TouchScope.FULL_ACCESS):
                        return vocabulary.translate("touch", mode.touch).native
                    if mode.approval is not None:
                        return vocabulary.translate("approval", mode.approval).native
                return mode.provider_mode or mode.id
        known = ", ".join(mode.id for mode in self.permission_modes()) or "none"
        raise ValueError(
            f"{intent!r} is not a permission intent {self.name!r} accepts (known: {known})"
        )

    def touch_of(self, intent: str | None) -> TouchScope | None:
        """Where a run given `intent` may change files (a shared touch intent, or one of this CLI's modes); None when
        unknown."""
        try:
            return TouchScope(intent) if intent is not None else None
        except ValueError:
            return next((mode.touch for mode in self.permission_modes() if mode.id == intent), None)

    def permission_intent(self, intent: str | None) -> PermissionMode | None:
        if intent is None:
            return None
        for mode in self.permission_modes():
            if mode.id == intent:
                return mode
        self.provider_permission_mode(intent)  # raises the standard diagnostic
        return None

    def provider_approval_mode(self, intent: str | None) -> str | None:
        """Translate the standard approval axis, independently of touch scope."""
        mode = self.permission_intent(intent)
        if mode is None or mode.approval is None:
            return None
        # ``None`` is meaningful here: some vendors have no flag for their safe default. Do not
        # turn a conceptual value such as `on-request` into an invented CLI argument.
        translation = vocabulary_for(self.name).approval[mode.approval]
        return translation.native

    def provider_thinking_level(self, reasoning: str | None) -> str | None:
        """Translate standard reasoning effort at the provider boundary."""
        if reasoning is None:
            return None
        try:
            level = ThinkingLevel(reasoning)
        except ValueError as exc:
            raise ValueError(f"{reasoning!r} is not a standard thinking intent") from exc
        return vocabulary_for(self.name).translate("thinking", level).native

    def validate_permission_mode(self, mode: str | None) -> None:
        """Validate a shared caller intent before policy or command construction."""
        self.provider_permission_mode(mode)

    def _permission_flag(self, mode: str | None) -> list[str]:
        """``--permission-mode <mode>`` when one was chosen, after checking it is one of ours.

        The value arrives from a tool caller and ends up on a command line, so an unknown one is
        refused at the edge rather than passed through: a caller could otherwise smuggle a second
        flag in through this field. No mode means no flag at all — the person's own CLI default
        must stay reachable, and overriding it silently would be its own defect.
        """
        native_mode = self.provider_permission_mode(mode)
        return [self.permission_option, native_mode] if native_mode is not None else []

    def sandbox_refusal(self) -> str | None:
        """Why this CLI's own OS sandbox cannot start on this PC (cause and fix), or None.

        A CLI whose every command runs in a sandbox that cannot start can do nothing: it is
        refused before launch, never left to tell the model its tools failed."""
        return None

    def image_attachment_support(self) -> bool:
        """Whether this installed provider can receive images with its initial prompt (a flag, or its stdin message)."""
        return self.can_attach_images or self.stdin_images

    def _image_args(self, image_paths: tuple[Path, ...]) -> list[str]:
        if not image_paths:
            return []
        if not self.image_attachment_support():
            raise ValueError(f"{self.name} provider does not support image attachments")
        return ["--image", *(str(path) for path in image_paths)]

    def definition_path(self, agent: str) -> Path | None:
        """The file holding a definition's system prompt, or None when this CLI has no such
        concept. A link to it is what makes "what IS this agent" answerable from a panel."""
        return None

    def valid_definition(self, agent: str) -> bool:
        """Whether ``agent`` names a definition this CLI actually has.

        Checked at the edge: the value arrives from a tool caller and ends up as a filesystem
        path the VS Code panel offers as a clickable link — a name like ``../../x`` would walk
        out of the definitions directory into something a person clicks. A CLI with no
        definitions concept accepts nothing, correctly: nothing for the name to resolve to.
        """
        catalog = AgentCatalog.active()
        if catalog is not None:
            return any(value.role_key == agent for value in catalog.snapshot.agents)
        if not agent or "/" in agent or "\\" in agent or agent.startswith("."):
            return False  # absolute: this becomes a path, and a path is what must not escape
        known = self.agent_definitions()
        # Enumeration is ADVISORY. A CLI that cannot list its definitions — none installed yet, a
        # layout we do not know how to read — must not thereby reject every name: that would turn
        # a hardening check into an outage. Unknown is only an error when we can prove it unknown.
        return agent in known if known else True

    def agent_definitions(self) -> list[str]:
        """Names this CLI can resolve as ``agent=``, sorted. Empty when it has no such concept.

        A caller — often an agent, which cannot ask a follow-up question — has no other way to
        learn which definitions exist, so a capability nobody can enumerate is unusable.
        """
        return []

    @abstractmethod
    def parse(self, line: str, ledger: UsageLedger | None = None) -> AgentEvent | None:
        """One stdout line → a normalised event, or None if the line carries nothing.

        ``ledger`` is the stream's usage state: pass ONE ledger for every line of a stream so
        token and cost figures sum to the vendor's truth. Without one the line is its own stream.
        """

    def resume_command(
        self, session_id: str, message: str, *, model: str | None = None,
        permission_mode: str | None = None, reasoning: str | None = None,
        agent: str | None = None, agent_prompt: str | None = None,
        mcp_config: str | None = None, allowed_tools: list[str] | None = None,
        denied_tools: tuple[str, ...] = (), coarse_accepted: bool = False,
        base_url: str | None = None, fork_to: str | None = None,
    ) -> list[str]:
        """The argv that delivers ``message`` into an existing session — or, with ``fork_to``, into
        a COPY of it under that new id (the original is never written)."""
        raise NotImplementedError(f"{type(self).__name__} cannot resume a session")

    #: The conversations this CLI saves itself, as a glob under its login folder (`auth_home`):
    #: the owner's editor sessions as well as the runs galaius started. Empty: it saves none galaius reads.
    transcript_glob: ClassVar[str] = ""

    def transcripts(self, home: Path) -> list[Path]:
        """The conversation files this CLI saved under its login folder `home`."""
        return list(home.glob(self.transcript_glob)) if self.transcript_glob else []

    def transcript_id(self, path: Path) -> str:
        """The session a saved conversation file holds, read from its name."""
        return path.stem

    def transcript_event(self, raw: dict) -> AgentEvent | None:
        """One record of a saved conversation file as an event (its `at` set); None for one that says nothing."""
        return None

    def transcript_place(self, raw: dict) -> tuple[str, str]:
        """(working folder, title) one record of a saved conversation file names; "" for each it does not."""
        return "", ""

    def transcript_branch(self, raw: dict) -> str:
        """The git branch one record of a saved conversation file says its session worked on; "" when it does not say."""
        return ""

    #: The origins (`transcript_origin`) of sessions a person types in — a terminal, an editor —
    #: as this CLI names them; its headless, SDK and sub-agent sessions are an agent's.
    typed_origins: ClassVar[frozenset[str]] = frozenset()

    def transcript_origin(self, raw: dict) -> str:
        """Where one record of a saved conversation file says its session came from (the program, or
        how it was opened); "" when it does not say."""
        return ""

    def transcript_typed(self, raw: dict) -> bool | None:
        """Whether one record's session is one a person types in; None when the record does not say."""
        origin = self.transcript_origin(raw)
        return origin in self.typed_origins if origin else None

    def resume_ahead_command(self, session_id: str, **arguments) -> list[str]:
        """`resume_command`'s child with no message: it starts now and reads the message
        `task_message` writes (`starts_ahead`)."""
        raise UnsupportedToolPolicy(f"{self.name} cannot resume before its message")

    def turn_preface(self, argv: Sequence[str]) -> bytes:
        """What galaius writes into a turn's stream ahead of the CLI's, from the turn's `argv`: nothing by default."""
        return b""

    def took_message(self, line: str) -> bool:
        """Whether `line`, written by a turn's child after its message was handed to it (on its
        command line or its stdin), says it took it: anything but its start's bookkeeping."""
        try:
            return self.parse(line) is not None
        except ValueError:
            return False

    def discover(self) -> list[dict]:
        """Agent sessions this provider can see that galaius did NOT spawn — the user's own
        interactive windows included. Optional; a provider with no such view returns []."""
        return []


def provider_caveat_note(provider: "AgentProvider") -> str:
    """One line naming what a caller still needs to know about this adapter, or "".

    ``verified`` and ``caveat`` answer different questions: verified says whether the flags were
    checked against a real binary; caveat says what remains true regardless. A provider exercised
    end to end (verified) can still carry a PERMANENT limitation (codex: no per-tool flag for a
    native tool) — hiding it the moment verified flips true silently drops the one thing a caller
    still needs, right when it looks most trustworthy. Shared by every surface that prints this
    (`agent_providers`, the CLI `providers` command, the per-spawn NOTE), so they cannot drift.
    """
    if provider.caveat:
        return f" — {provider.caveat}"
    if not provider.verified:
        return " — unverified: not yet exercised against a real binary"
    return ""


#: `system` subtypes that describe the HARNESS rather than the agent — token accounting and hook
#: lifecycle. They outnumbered the real steps in the activity view, burying the transparency the
#: panel exists for. Only known noise is dropped; an unrecognised subtype still comes through as
#: `other`, so a vendor adding an event cannot vanish silently.
_HARNESS_BOOKKEEPING = frozenset({"hook_started", "hook_response"})

#: How Claude Code opens the `isSynthetic` user turn carrying a Stop hook's block.
_STOP_HOOK_FEEDBACK = "Stop hook feedback:"

class ClaudeTurnSettings(BaseModel):
    """What galaius sets in each agent turn of Claude Code it launches, as `--settings`: on 2.1.288 they win over the
    user's own settings files, whatever `--setting-sources` loads. Values: the role's `compact_at` (`Policy.compact_at_for`),
    `Config.claude_bash_output_chars`."""

    model_config = ConfigDict(frozen=True)

    #: What Claude Code keeps free below its `autoCompactWindow`: it compacts at the window minus this. Probed 2.1.288
    #: (`get_context_usage`): window 153000 → `autoCompactThreshold` 120000; a window under 100000 is ignored (the user's
    #: own setting applies), which `CompactAt`'s floor keeps clear of.
    COMPACT_RESERVE: ClassVar[int] = 33_000

    #: The context, in tokens, it compacts the conversation at.
    compact_at: int
    #: The characters of a Bash command's output kept in the conversation (`bashOutputMaxChars`); the rest goes to a
    #: file the agent is pointed at.
    bash_output_chars: int
    #: Run after each of the turn's own tool calls (`PostToolUse`; what it prints as `additionalContext` the model reads
    #: before its next step), with the note telling the agent which of that context is the person's (`galaius.inbox_hook.NOTE`).
    inbox_hook: str | None = None
    #: Run once a compaction replaced the conversation (`SessionStart`, source `compact`: `galaius.brief_hook`), with the
    #: note telling the agent what that context is (`galaius.brief_hook.NOTE`).
    brief_hook: str | None = None
    #: The name a peer session's SendMessage addresses it by (`-n`): `<role>-<short run id>`. Unnamed, the CLI names a session
    #: after its folder (`<folder>-<2 hex>`) and a resume without `-n` drops the name: no peer finds a run by its role.
    name: str
    #: No MCP server but the one galaius hands it (`--strict-mcp-config`): none the person configured for himself, so no
    #: credential of his reaches it through one (a community agent, `galaius.agents.community`).
    isolated: bool = False

    def arguments(self) -> list[str]:
        hooks = {
            **({"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": self.inbox_hook}]}]} if self.inbox_hook else {}),
            **({"SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": self.brief_hook}]}]} if self.brief_hook else {}),
        }
        settings = {"autoCompactWindow": self.compact_at + self.COMPACT_RESERVE, "bashOutputMaxChars": self.bash_output_chars, **({"hooks": hooks} if hooks else {})}
        notes = [note for hook, note in ((self.inbox_hook, INBOX_NOTE), (self.brief_hook, BRIEF_NOTE)) if hook]
        return (["-n", self.name, "--settings", json.dumps(settings)] + (["--append-system-prompt", "\n\n".join(notes)] if notes else [])
                + (["--strict-mcp-config"] if self.isolated else []))


class ClaudeCodeProvider(AgentProvider):
    """Claude Code, driven through its documented headless mode.

    Flags verified against `claude --version` 2.1.233. `--output-format stream-json` REQUIRES
    `--verbose`; without it the CLI refuses and the run emits nothing parseable.
    """

    name = "claude"
    folder_settings_off = ("--setting-sources", "user")
    reads_mid_turn = True
    takes_turn_settings = True
    starts_ahead = True
    stdin_images = True
    # 2.1.288, headless stream-json: a resumed turn or a stdin turn of exactly this text compacts
    # (`compact_boundary`, trigger `manual`).
    compact_prompt = "/compact"
    native_providers = frozenset({"anthropic"})
    media_model_field = "claude_media_criteria"
    auth_home_env = ("CLAUDE_CONFIG_DIR",)
    home_folder = ".claude"
    # 2.1.288 headless (read from its own code, undocumented: check again on a CLI upgrade): unset, a turn starts before a
    # server still connecting answers (its init reads it `pending`, its tools absent from that turn); set above 0, the
    # turn holds until each server connected or the time passed.
    mcp_wait_variable = "CLAUDE_CODE_MCP_STARTUP_WAIT_MS"
    # 2.1.288 says its status (`api_error_status`); before it, only « Failed to authenticate: OAuth session expired and
    # could not be refreshed », « …: OAuth token revoked » (another account signed in), its docs « Login expired · Please run /login ».
    cut_words = {RunCut.login_lost: re.compile(r"OAuth session expired|OAuth token (?:has expired|revoked)|Login expired|Please run /login|Not logged in", re.IGNORECASE),
                 **AgentProvider.cut_words}
    settings_file = "settings.json"
    no_extra_usage_guidance = (
        "in Claude Settings → Usage, keep Usage credits disabled, ensure prepaid balance is zero, "
        "and turn auto-reload off"
    )

    def can_run(self, model: Model, env: dict[str, str]) -> bool:
        if super().can_run(model, env):
            return True
        # Claude Code honours ANTHROPIC_BASE_URL, so a provider galaius knows the Anthropic-wire
        # endpoint of (ollama) is reachable too — when that provider is actually available here.
        # Checked by KEY, not truthiness: overlay_for also returns a non-empty, OPENAI-shaped dict
        # for hf/vllm, which Claude Code cannot speak at all.
        return "ANTHROPIC_BASE_URL" in overlay_for(f"{model.provider}/{model.id}", env) and model.is_available()
    binary = "claude"
    session_media_kinds = frozenset({"image", "video"})
    can_resume = True
    installers = {
        "posix": ProgramInstaller(url="https://claude.ai/install.sh", hosts=ANTHROPIC_INSTALL_HOSTS, shell=("bash",), bin_dirs=("~/.local/bin",)),
        "windows": ProgramInstaller(url="https://claude.ai/install.ps1", hosts=ANTHROPIC_INSTALL_HOSTS, shell=POWERSHELL_FILE, bin_dirs=("~/.local/bin",)),
    }
    #: Calls `BROWSER` with a sign-in page that comes back to this PC (a localhost callback), and prints one for any
    #: browser whose page shows a code, then waits for that code (2.1.288: « If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?…
    #: redirect_uri=https://platform.claude.com/oauth/code/callback… », then « Paste code here if prompted > »).
    login_arguments = ("auth", "login")
    paste_link = re.compile(r"visit:\s*(https://\S+)")
    #: `<code>#<state>`: given only the part before `#` it says « Invalid code. Please make sure the full code was copied. »
    #: and waits for another (2.1.288).
    paste_code = re.compile(r"^[^#\s]+#[^#\s]+$")
    logout_arguments = ("auth", "logout")
    #: `rate_limit_info.unifiedWindows` keys and the window each is (utilization: a share, 0-1).
    #: `seven_day_overage_included` is the same week with the overage pool counted: not the plan's own.
    WINDOWS: ClassVar[dict[str, UsageWindowName]] = {"five_hour": "five_hour", "seven_day": "weekly"}

    def usage(self, line: dict) -> tuple[UsageWindow, ...]:
        info = line.get("rate_limit_info") if line.get("type") == "rate_limit_event" else None
        windows = info.get("unifiedWindows") if isinstance(info, dict) else None
        if not isinstance(windows, dict):
            return ()
        now = datetime.now(UTC)
        return tuple(
            UsageWindow(window=self.WINDOWS[key], used_percent=round(float(value["utilization"]) * 100, 2), observed_at=now,
                        resets_at=datetime.fromtimestamp(value["resetsAt"], UTC) if isinstance(value.get("resetsAt"), (int, float)) else None)
            for key, value in windows.items()
            if key in self.WINDOWS and isinstance(value, dict) and isinstance(value.get("utilization"), (int, float))
        )

    #: The id its usage request answers under (`usage_command`).
    USAGE_REQUEST: ClassVar[str] = "galaius-usage"

    def usage_command(self) -> tuple[list[str], tuple[dict, ...]]:
        """Its `get_usage` control request (2.1.288: its SDK's `usage_EXPERIMENTAL…`, the `/usage` screen's data): the
        plan's windows as claude.ai has them; `skip_behaviors` skips its scan of the local transcripts (1-2 s instead of
        15). No session kept, no settings, hooks, plugins or MCP servers loaded: no model turn, no transcript, no child.
        Probed 2026-10-13: 0 tokens, ~1 s of CPU, no child process."""
        return ([self.executable(), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                 "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config"],
                ({"type": "control_request", "request_id": self.USAGE_REQUEST, "request": {"subtype": "get_usage", "skip_behaviors": True}},))

    @staticmethod
    def _answer(line: dict, request: str) -> dict | None:
        """What `line` answers to control request `request`, when it is its success."""
        response = line.get("response") if line.get("type") == "control_response" else None
        if not isinstance(response, dict) or response.get("request_id") != request or response.get("subtype") != "success":
            return None
        answer = response.get("response")
        return answer if isinstance(answer, dict) else None

    def usage_answer(self, line: dict) -> tuple[UsageWindow, ...] | None:
        """`response.response.rate_limits`: `five_hour` / `seven_day` {utilization 0-100, resets_at ISO}; none when the
        login has no plan windows (signed out, an API key: `rate_limits_available` false). An error (the vendor
        unreachable) says nothing: the last reading stands."""
        if (answer := self._answer(line, self.USAGE_REQUEST)) is None:
            return None
        limits = answer.get("rate_limits")
        if not isinstance(limits, dict):
            return () if answer.get("rate_limits_available") is False else None
        now = datetime.now(UTC)
        return tuple(UsageWindow(window=self.WINDOWS[key], used_percent=float(value["utilization"]), observed_at=now, resets_at=self._moment(value.get("resets_at")))
                     for key, value in limits.items() if key in self.WINDOWS and isinstance(value, dict) and isinstance(value.get("utilization"), (int, float)))

    @staticmethod
    def _moment(value: object) -> datetime | None:
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def model_id_for(self, model: Model) -> str:
        """Derive Claude Code's hyphenated version spelling for every future catalog row."""
        model_id = super().model_id_for(model)
        return re.sub(r"(?<=\d)\.(?=\d)", "-", model_id)

    def auth_command(self) -> list[str]:
        return [self.executable(), "auth", "status"]

    # Its JSON status keeps `loggedIn: true` for a claude.ai login that expired and could not be
    # refreshed; its text status then says « Login: Expired — log in again » and exits 1 (2.1.288).
    expired_said = re.compile(r"^Login:\s*Expired\b", re.IGNORECASE | re.MULTILINE)

    def expiry_command(self) -> list[str]:
        return [self.executable(), "auth", "status", "--text"]

    def media_help_text(self) -> str:
        """Installed parser vocabulary only; this never authenticates or starts a model turn."""
        completed = subprocess.run(
            [self.executable(), "--help"], capture_output=True, text=True, timeout=10, check=False
        )
        if completed.returncode:
            raise RuntimeError("Claude CLI help is unavailable")
        return completed.stdout

    def accepts_subscription_auth(self, stdout: str, stderr: str) -> bool:
        try:
            status = json.loads(stdout)
        except ValueError:
            return False
        return bool(
            isinstance(status, dict)
            and status.get("loggedIn") is True
            and status.get("authMethod") == "claude.ai"
        )

    def accepts_any_auth(self, stdout: str, stderr: str) -> bool:
        try:
            status = json.loads(stdout)
        except ValueError:
            return False
        return isinstance(status, dict) and status.get("loggedIn") is True

    #: Its JSON status's, never `configDirectory` / `projectsDirectory` (where it is kept) nor `subscriptionType`.
    IDENTITY_KEYS = ("authMethod", "apiProvider", "email", "orgId")

    def signed_in_as(self, stdout: str, stderr: str) -> str:
        try:
            status = json.loads(stdout)
        except ValueError:
            return ""
        said = [str(status[key]) for key in ("email", "orgName") if isinstance(status, dict) and status.get(key)]
        return " · ".join(said)[:200]

    def login_identity(self, stdout: str, stderr: str) -> str:
        try:
            return self.identity_of(json.loads(stdout))
        except ValueError:
            return ""

    def supports_session_media(self, media_kind: str) -> bool:
        return media_kind in self.session_media_kinds

    async def media_isolation_args(
        self, env: dict[str, str], *, cwd: Path, timeout: float
    ) -> tuple[str, ...]:
        return ()

    async def run_media_process(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        stdin: bytes | None = None,
    ) -> list[AgentEvent]:
        started = time.monotonic()
        try:
            returncode, stdout_bytes, stderr_bytes = await run_isolated_process(
                argv, cwd=cwd, env=env, timeout=timeout, stdin=stdin
            )
        except TimeoutError as exc:
            raise _MediaProcessTimeout(
                f"{self.name} media session timed out after {timeout:g}s",
                timeout_phase="provider_media",
                elapsed_seconds=time.monotonic() - started,
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"{self.name} CLI could not start: {exc.strerror or exc}") from exc
        ledger = UsageLedger()
        events = [
            event
            for line in stream_lines(stdout_bytes.decode(errors="replace"))
            if (event := self.parse(line, ledger))
        ]
        facts = _failure_event_facts(events)
        failure_kwargs = {
            "events": facts,
            "stderr_bytes": len(stderr_bytes),
            "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
            "elapsed_seconds": time.monotonic() - started,
        }
        if returncode:
            raise _MediaProcessFailure(
                f"{self.name} media session exited {returncode}",
                exit_code=returncode,
                **failure_kwargs,
            )
        errors = [event for event in events if event.kind in ("error", "rate_limit")]
        if errors:
            category = "subscription quota or rate limit" if errors[-1].kind == "rate_limit" else "provider reported failure"
            raise _MediaProcessFailure(f"{self.name} media session failed: {category}", **failure_kwargs)
        if not any(event.kind == "done" for event in events):
            raise _MediaProcessFailure(
                f"{self.name} media session exited without a final result", **failure_kwargs
            )
        if not any(
            (event.kind == "text" or event.final_text) and event.text.strip()
            for event in events
        ):
            raise _MediaProcessFailure(
                f"{self.name} media session returned no final message", **failure_kwargs
            )
        return events

    async def media_cli_version(
        self, env: dict[str, str], *, cwd: Path, timeout: float
    ) -> str:
        try:
            returncode, stdout, _ = await run_isolated_process(
                [self.executable(), "--version"], cwd=cwd, env=env, timeout=timeout
            )
        except (OSError, TimeoutError):
            return "unavailable"
        line = stdout.decode(errors="replace").splitlines()[0].strip() if stdout else ""
        if returncode or re.fullmatch(r"[A-Za-z0-9 ._+()/-]{1,120}", line) is None:
            return "unavailable"
        return line

    def media_result(self, events: list[AgentEvent]) -> _MediaResult:
        terminal = next(event for event in reversed(events) if event.kind == "done")
        text = terminal.text if terminal.final_text else next(
            event.text for event in reversed(events)
            if event.kind == "text" and event.text.strip()
        )
        costs = [event.cost_usd for event in events if event.cost_usd is not None]
        return _MediaResult(
            text=text,
            input_tokens=sum(event.input_tokens or 0 for event in events),
            output_tokens=sum(event.output_tokens or 0 for event in events),
            reported_cost_usd=sum(costs) if costs else None,
        )

    def media_command(
        self,
        *,
        cwd: Path,
        model: str | None,
        media_paths: list[Path],
        schema_path: Path | None,
        schema_json: str | None,
        mcp_config: Path,
        settings_path: Path,
        isolation_args: tuple[str, ...] = (),
    ) -> list[str]:
        argv = [
            self.executable(), "-p",
            "--output-format", "stream-json", "--verbose",
            "--safe-mode", "--no-session-persistence",
            "--permission-mode", "dontAsk",
            "--mcp-config", str(mcp_config), "--strict-mcp-config",
            "--settings", str(settings_path),
        ]
        if media_paths:
            if any(any(char in str(path) for char in "*?[](){},") for path in media_paths):
                raise ValueError("Claude media staging path contains permission-rule metacharacters")
            rules = ",".join(f"Read({path})" for path in media_paths)
            argv += ["--tools", "Read", "--allowedTools", rules]
        else:
            argv += ["--tools", ""]
        if model:
            argv += ["--model", model]
        if schema_json:
            argv += ["--json-schema", schema_json]
        return argv

    #: Read off `claude --help` on the INSTALLED binary (2.1.233), not from memory of the docs —
    #: which would have produced "default" and missed auto/manual/dontAsk entirely.
    _MODES: ClassVar[tuple[PermissionMode, ...]] = (
        PermissionMode("plan", "Plan only",
                       "works out an approach and touches nothing",
                       touch=TouchScope.READ_ONLY, approval=ApprovalIntent.NEVER),
        PermissionMode("manual", "Ask every time",
                       "you approve each action before it happens",
                       touch=TouchScope.WORKSPACE_WRITE, approval=ApprovalIntent.ASK),
        PermissionMode("auto", "Ask when it matters",
                       "handles the routine, asks about the rest",
                       touch=TouchScope.WORKSPACE_WRITE, approval=ApprovalIntent.AUTO_SAFE),
        PermissionMode("acceptEdits", "May edit files",
                       "file changes go through, other actions still ask",
                       touch=TouchScope.WORKSPACE_WRITE, approval=ApprovalIntent.AUTO_EDITS),
        PermissionMode("dontAsk", "Stop asking",
                       "no prompts; declines what it is not allowed to do",
                       touch=TouchScope.WORKSPACE_WRITE, approval=ApprovalIntent.NEVER),
        PermissionMode("bypassPermissions", "No restrictions",
                       "acts without asking, including outside the workspace",
                       unrestricted=True, touch=TouchScope.FULL_ACCESS, approval=ApprovalIntent.NEVER),
    )

    def permission_modes(self) -> list[PermissionMode]:
        return list(self._MODES)

    def validate_tool_policy(self, allowed_tools: list[str] | None, denied_tools: tuple[str, ...], *,
                             coarse_accepted: bool = False) -> None:
        # Claude enforces the FULL list itself (`--agents`/`--allowedTools`/`--disallowedTools`
        # below); there is nothing coarser to fall back to, so acceptance of one is moot here.
        validate_denied_tools(denied_tools)

    def role_arguments(self, agent: str | None, agent_prompt: str | None,
                       allowed_tools: list[str] | None, denied_tools: tuple[str, ...]) -> list[str]:
        self.validate_tool_policy(allowed_tools, denied_tools)
        arguments = []
        if agent is not None:
            self.validate_agent_name(agent)
        if allowed_tools is not None or agent_prompt is not None:
            if not agent:
                raise UnsupportedToolPolicy("A named role is required to restrict Claude tools")
            if agent_prompt is None:
                agent_prompt = self.definition_prompt(agent)
            definition = {"description": f"Galaius role {agent}", "prompt": agent_prompt}
            if allowed_tools is not None:
                definition["tools"] = allowed_tools
            arguments += ["--agents", json.dumps({agent: definition})]
        if agent:
            arguments += ["--agent", agent]
        if no_tools(allowed_tools):
            # No tool at all: no built-in one, and no MCP server — not even one the person configured for himself
            # (2.1.288: `--tools ""` alone keeps every MCP server's tools; `--strict-mcp-config` with no config loads none).
            arguments += ["--tools", "", "--strict-mcp-config"]
        if denied_tools:
            arguments += ["--disallowedTools", ",".join(denied_tools)]
        return arguments

    @staticmethod
    def _mcp_arguments(mcp_config: str | None, allowed_tools: list[str] | None) -> list[str]:
        """`--mcp-config`, never for a role with no tool at all (`role_arguments`)."""
        return ["--mcp-config", mcp_config] if mcp_config and not no_tools(allowed_tools) else []

    def command(self, task: str, **arguments) -> list[str]:
        # The prompt goes LAST, after "--": one starting with "-" (a markdown bullet) is the
        # prompt, never an option Claude refuses ("unknown option").
        return [*self._options(**arguments), "--", task]

    def _options(self, *, cwd: str, model: str | None, mcp_config: str | None,
                run_id: str, agent: str | None = None,
                permission_mode: str | None = None,
                allowed_tools: list[str] | None = None,
                reasoning: str | None = None,
                agent_prompt: str | None = None, denied_tools: tuple[str, ...] = (),
                image_paths: tuple[Path, ...] = (), coarse_accepted: bool = False,
                base_url: str | None = None, environment: Mapping[str, str] | None = None,
                turn: ClaudeTurnSettings | None = None) -> list[str]:
        # `base_url` is unused here: Claude Code only ever speaks the Anthropic wire protocol
        # (ANTHROPIC_BASE_URL, read from its own process env, set by resolve_model's overlay) —
        # never OpenAI's, so a routed hf/vllm endpoint is never handed to this binary at all
        # (see CodexProvider.can_run / ClaudeCodeProvider.can_run's ANTHROPIC_BASE_URL check).
        if image_paths:
            # Its command line carries no image: a shown image reaches this CLI inside its stdin message
            # (`stdin_images`, `task_message`). Refused here rather than dropped on the way in.
            raise ValueError(f"{self.name} provider takes image attachments in its stdin message, not on its command line")
        argv = [
            self.binary, "-p",
            "--output-format", "stream-json",
            "--verbose",  # mandatory companion to stream-json
            # Our run id IS the vendor's session id, so `claude --resume <run_id>` works and no
            # mapping table can go stale.
            "--session-id", run_id,
        ]
        if model:
            argv += ["--model", model]
        argv += self.role_arguments(agent, agent_prompt, allowed_tools, denied_tools) + self._mcp_arguments(mcp_config, allowed_tools)
        argv += self._permission_flag(permission_mode) + self._setting_sources(environment) + (turn.arguments() if turn else [])
        if reasoning is not None:
            argv += ["--effort", self.provider_thinking_level(reasoning)]
        return argv

    def ahead_command(self, **arguments) -> list[str]:
        """`command`'s options, the task read as stream-json input instead: the CLI does its startup
        (settings, hooks, agents, MCP servers) at once and then waits for it."""
        return [*self._options(**arguments), "--input-format", "stream-json"]

    #: The id its context request answers under (`task_message`).
    CONTEXT_REQUEST: ClassVar[str] = "galaius-context"
    #: The `type` of the line galaius writes ahead of each turn's stream (`turn_preface`).
    TURN_LINE: ClassVar[str] = "galaius_turn"

    def turn_preface(self, argv: Sequence[str]) -> bytes:
        """The compaction window this turn's `argv` sets (`ClaudeTurnSettings`'s `--settings`), as a line ahead of the
        CLI's own: a replay of the stream reads what each turn ran under. Nothing for a turn that sets none (an editor
        conversation's: its owner's own settings apply)."""
        options = list(argv[:argv.index("--")] if "--" in argv else argv)
        try:
            window = json.loads(options[options.index("--settings") + 1]).get("autoCompactWindow")
        except (ValueError, IndexError, AttributeError):  # no `--settings`, or a settings file's path (`media_command`)
            window = None
        return (json.dumps({"type": self.TURN_LINE, "compact_window": window}) + "\n").encode() if isinstance(window, int) else b""

    def task_message(self, task: str, images: tuple[Path, ...] = ()) -> bytes:
        """Its stream-json user message — `task` alone, or a text block (when `task` says something) then one base64
        image block per image — after a `get_context_usage` control request (2.1.288, its SDK's `getContextUsage`),
        answered before the turn starts: `maxTokens` is the window this CLI sizes the conversation to (by model,
        account and flags: 200k or 1M for one model id), so a run's gauge has it from its start, not only from the
        turn's `result`. Probed 2026-10-13: answered before `init`, 0 tokens; the same window as `result.modelUsage`
        (haiku-5-5 200k, opus-5-5, opus-5-5[1m], sonnet-5 1M)."""
        shown = tuple(AgentMedia.of(path.read_bytes()) for path in images)
        content = task if not shown else [*([{"type": "text", "text": task}] if task.strip() else []),
                                          *({"type": "image", "source": {"type": "base64", "media_type": image.content_type, "data": image.data}} for image in shown)]
        lines = ({"type": "control_request", "request_id": self.CONTEXT_REQUEST, "request": {"subtype": "get_context_usage", "detail": "summary"}},
                 {"type": "user", "message": {"role": "user", "content": content}})
        return "".join(json.dumps(line) + "\n" for line in lines).encode()

    #: What Claude Code reads under the user's folder and under each enclosing folder's `.claude`.
    _SCOPE_FILES: ClassVar[tuple[str, ...]] = ("settings.json", "settings.local.json", "agents/*.md", "skills/*/SKILL.md", "commands/**/*.md", "rules/**/*.md")
    #: Instruction files it reads in each enclosing folder, and the `@path` imports they name.
    _INSTRUCTIONS: ClassVar[tuple[str, ...]] = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")
    _IMPORT: ClassVar[re.Pattern[str]] = re.compile(r"(?<![\w`])@((?:~/|\.{0,2}/)?[\w.-]+(?:/[\w.-]+)*)")

    def user_instructions(self, environment: Mapping[str, str]) -> Path:
        # `--setting-sources user` (folder settings off) still loads it.
        return self.auth_home(environment) / "CLAUDE.md"

    @staticmethod
    def project_folder(home: Path, cwd: Path) -> Path:
        """Where Claude Code keeps a folder's sessions and memory inside its login folder `home`."""
        return home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))

    def session_files(self, home: Path, cwd: Path, session: str) -> tuple[Path, ...]:
        """Its transcript, and the folder of that session's sub-agents and tool results beside it when there is one."""
        folder = self.project_folder(home, cwd)
        return (folder / f"{session}.jsonl", *(sorted(path for path in (folder / session).rglob("*") if path.is_file()) if (folder / session).is_dir() else ()))

    def startup_files(self, cwd: Path, environment: Mapping[str, str]) -> tuple[Path, ...]:
        """The user's and every enclosing folder's instructions (with the files they import),
        settings, MCP servers, agents, skills, commands and rules; the folder's memory index; the
        installed plugins. The user's `~/.claude.json` is left out: Claude rewrites it at every start."""
        home = self.auth_home(environment)
        folders = (cwd, *cwd.parents)
        scopes = (home, *(folder / ".claude" for folder in folders))
        instructions = [self.user_instructions(environment), *(folder / name for folder in folders for name in self._INSTRUCTIONS)]
        return (
            home / "plugins" / "installed_plugins.json",
            self.project_folder(home, cwd) / "memory" / "MEMORY.md",
            *(folder / ".mcp.json" for folder in folders),
            *(path for scope in scopes for pattern in self._SCOPE_FILES for path in sorted(scope.glob(pattern))),
            *instructions, *self._imports(instructions, home.parent),
        )

    def _imports(self, files: list[Path], user_home: Path, depth: int = 3) -> list[Path]:
        """The files `files` import (`@path`, relative to the importing file; `~/` the user's home),
        followed `depth` levels deep, as Claude Code follows them."""
        found: list[Path] = []
        for file in files:
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name in self._IMPORT.findall(text):
                found.append(user_home / name[2:] if name.startswith("~/") else file.parent / name)
        return found + (self._imports(found, user_home, depth - 1) if found and depth > 1 else [])

    def definition_path(self, agent: str) -> Path | None:
        catalog = AgentCatalog.active()
        if catalog is not None:
            return catalog.definition_path(agent)
        path = Path.home() / ".claude" / "agents" / f"{agent}.md"
        return path if path.exists() else None

    def agent_definitions(self) -> list[str]:
        """Claude Code resolves ``--agent <name>`` against ``~/.claude/agents/<name>.md``."""
        catalog = AgentCatalog.active()
        if catalog is not None:
            return sorted(value.role_key for value in catalog.snapshot.agents if value.role_key is not None)
        try:
            return sorted(p.stem for p in (Path.home() / ".claude" / "agents").glob("*.md"))
        except OSError:
            return []

    def resume_command(
        self, session_id: str, message: str, *, model: str | None = None,
        permission_mode: str | None = None, reasoning: str | None = None,
        agent: str | None = None,
        agent_prompt: str | None = None, allowed_tools: list[str] | None = None,
        denied_tools: tuple[str, ...] = (), mcp_config: str | None = None,
        coarse_accepted: bool = False, base_url: str | None = None, fork_to: str | None = None,
        turn: ClaudeTurnSettings | None = None,
    ) -> list[str]:
        """Continue an existing session using its provider session id (`fork_to`: as a copy)."""
        return [*self._resume_options(session_id, model=model, permission_mode=permission_mode, agent=agent, agent_prompt=agent_prompt, allowed_tools=allowed_tools,
                                      denied_tools=denied_tools, mcp_config=mcp_config, fork_to=fork_to, turn=turn), "--", message]

    def resume_ahead_command(self, session_id: str, **arguments) -> list[str]:
        """`resume_command`'s options, the message read as stream-json input: the CLI loads the
        session and does its startup at once, then waits (before its message it writes only its
        start hooks' bookkeeping lines). It echoes the message once it has read it (`isReplay`):
        the mark that it took it, which a launcher stopped mid-hand-over reads (`galaius.agents.followup`)."""
        return [*self._resume_options(session_id, **arguments), "--input-format", "stream-json", "--replay-user-messages"]

    def took_message(self, line: str) -> bool:
        """Also its echo of a message read on stdin (`resume_ahead_command`), which the parser drops."""
        try:
            raw = json.loads(line)
        except ValueError:
            return False
        # Its answer to the context request comes before it reads the message.
        return isinstance(raw, dict) and raw.get("type") != "control_response" and (
            (raw.get("type") == "user" and raw.get("isReplay") is True) or super().took_message(line))

    def _resume_options(
        self, session_id: str, *, model: str | None = None, permission_mode: str | None = None, reasoning: str | None = None,
        agent: str | None = None, agent_prompt: str | None = None, allowed_tools: list[str] | None = None, denied_tools: tuple[str, ...] = (),
        mcp_config: str | None = None, coarse_accepted: bool = False, base_url: str | None = None, fork_to: str | None = None,
        turn: ClaudeTurnSettings | None = None,
    ) -> list[str]:
        # base_url unused — see command()'s docstring note; reasoning travels as CLAUDE_CODE_EFFORT_LEVEL.
        return [
            self.binary, "-p",
            "--resume", session_id,
            *(["--fork-session", "--session-id", fork_to] if fork_to else []),
            "--output-format", "stream-json",
            "--verbose",
        ] + (["--model", model] if model else []) + self._permission_flag(permission_mode) + self._setting_sources() + (turn.arguments() if turn else []) + self.role_arguments(agent, agent_prompt, allowed_tools, denied_tools) + self._mcp_arguments(mcp_config, allowed_tools)

    #: Claude's two usage dialects: ``message.usage`` / ``result.usage`` (snake) and
    #: ``result.modelUsage[model]`` (camel). Each reports the UNCACHED prompt remainder, cache
    #: reads and cache writes as three ADDITIVE parts — a fully cached 102k prompt says 2 in
    #: ``input_tokens`` — so the contract's prompt total is their sum.
    _USAGE_KEYS: ClassVar[dict[Literal["snake", "camel"], tuple[str, str, str, str]]] = {
        "snake": ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                  "output_tokens"),
        "camel": ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
                  "outputTokens"),
    }

    @classmethod
    def _usage(
        cls, report: object, dialect: Literal["snake", "camel"] = "snake"
    ) -> TokenUsage | None:
        """One Claude usage object, or None when it reports no prompt part at all (zero would
        read as a measured empty context rather than an absence)."""
        if not isinstance(report, dict):
            return None
        uncached, read, write, output = (report.get(key) for key in cls._USAGE_KEYS[dialect])
        if not any(isinstance(part, int) for part in (uncached, read, write)):
            return None
        read, write = (part if isinstance(part, int) else 0 for part in (read, write))
        return TokenUsage(
            input_tokens=(uncached if isinstance(uncached, int) else 0) + read + write,
            cached_input_tokens=read, cache_write_input_tokens=write,
            output_tokens=output if isinstance(output, int) else 0,
        )

    def _settle_result(self, raw: dict, ledger: UsageLedger) -> tuple[TokenUsage | None, float | None]:
        """The invocation's share of the truth, from its ``result`` line.

        ``result.usage`` covers only the main thread of THIS invocation; ``result.modelUsage``
        and ``total_cost_usd`` cover every model call — internal ones (a haiku title or
        summary) and subagents that never stream an ``assistant`` line — cumulatively across a
        resumed session when Claude restored its counters. The ledger reconciles the two.
        """
        local = self._usage(raw.get("usage"))
        per_model = {
            model: usage for model, report in (raw.get("modelUsage") or {}).items()
            if (usage := self._usage(report, "camel")) is not None
        }
        if not per_model and local is not None:
            per_model = {ledger.primary or "": local}
        cost = raw.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        if not per_model:
            return None, cost
        return ledger.settle(per_model, cost, local)

    transcript_glob = "projects/*/*.jsonl"

    def parse(self, line: str, ledger: UsageLedger | None = None) -> AgentEvent | None:
        line = line.strip()
        if not line:
            return None
        try:
            raw = json.loads(line)
        except ValueError:
            return None  # a non-JSON line (a warning on stdout) is noise, not an event
        if not isinstance(raw, dict):
            return None
        return self.event(raw, UsageLedger() if ledger is None else ledger)

    def transcript_event(self, raw: dict) -> AgentEvent | None:
        """A saved session's record reads as its stream line does; `isMeta` records are the harness's own
        notes, `isCompactSummary` ones its summary of the turns before (in the person's turn, not his words)."""
        if raw.get("isMeta") or raw.get("isCompactSummary"):
            return None
        event = self.event(raw, UsageLedger())
        return None if event is None else event.model_copy(update={"at": instant(raw.get("timestamp"))})

    def transcript_place(self, raw: dict) -> tuple[str, str]:
        return str(raw.get("cwd") or ""), str(raw.get("aiTitle") or "") if raw.get("type") == "ai-title" else ""

    def transcript_branch(self, raw: dict) -> str:
        return str(raw.get("gitBranch") or "")

    #: Its `CLAUDE_CODE_ENTRYPOINT` values (as its CLI names them) for the terminal, the editor extension
    #: and the desktop app; headless runs save `sdk-cli` / `sdk-ts` / `sdk-py`.
    typed_origins = frozenset({"cli", "claude-vscode", "claude-desktop"})

    def transcript_origin(self, raw: dict) -> str:
        return str(raw.get("entrypoint") or "")

    def event(self, raw: dict, ledger: UsageLedger) -> AgentEvent | None:
        """One decoded stream line (or saved-session record) as an event."""
        kind = raw.get("type", "")
        sid = raw.get("session_id")

        if kind == self.TURN_LINE:
            ledger.compact_window = raw.get("compact_window") if isinstance(raw.get("compact_window"), int) else None
            return None  # galaius's own line: what the turn runs under, not what the agent did

        if kind == "system" and raw.get("subtype") in _HARNESS_BOOKKEEPING:
            return None  # about the harness's own machinery, not about what the agent did

        if kind == "system" and raw.get("subtype") == "thinking_tokens":
            # Its live estimate while the model thinks (2.1.288: one line per ~150-250 tokens): counted for the run's gauge
            # (`StreamMirror`), never a transcript entry; the turn's result settles it.
            ledger.think(raw.get("estimated_tokens_delta"))
            return None

        if kind == "system" and raw.get("subtype") == "task_started":
            return AgentEvent(kind="spawn", session_id=sid, raw_type=kind,
                              text=str(raw.get("description") or raw.get("agent_type") or "subagent"))

        if kind == "system" and raw.get("subtype") == "compact_boundary":
            facts = raw.get("compact_metadata") or {}
            trigger, before, after = (facts.get(key) for key in ("trigger", "pre_tokens", "post_tokens"))
            return AgentEvent(kind="compacted", session_id=sid, raw_type=kind,
                              compaction=trigger if trigger in get_args(CompactionTrigger) else None,
                              compacted_from=before if isinstance(before, int) else None,
                              **ContextGauge.reported({"context_tokens": after}))

        if kind == "control_response":
            answer = self._answer(raw, self.CONTEXT_REQUEST) or {}
            if isinstance(answer.get("maxTokens"), int):
                ledger.window = answer["maxTokens"]
            return AgentEvent(kind="other", session_id=sid, raw_type=kind, **ContextGauge.reported({"context_window": answer.get("maxTokens")}))

        if kind == "system" and raw.get("subtype") == "init":
            tools = raw.get("tools") or []
            if isinstance(raw.get("model"), str):
                ledger.primary = raw["model"]
            return AgentEvent(kind="started", session_id=sid, raw_type=kind,
                              text=f"session up in {raw.get('cwd', '?')} ({len(tools)} tools)")

        if kind == "assistant":
            msg = raw.get("message") or {}
            texts, thinking, tool, tool_input, tool_id = [], [], None, "", ""
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    texts.append(block.get("text", ""))
                elif btype == "thinking":
                    thinking.append(block.get("thinking", ""))
                elif btype == "tool_use":
                    tool = block.get("name")
                    tool_input = _summarise_input(block.get("input"))
                    tool_id = str(block.get("id") or "")
            # Claude repeats one message's usage on the line of EVERY content block, and its
            # output_tokens there is the stream-start value. Only the prompt side is a live
            # figure, counted once per message id; the result line settles the rest.
            usage = self._usage(msg.get("usage"))
            observed = None if usage is None else ledger.observe(
                msg.get("id") if isinstance(msg.get("id"), str) else None,
                usage.model_copy(update={"output_tokens": 0}),
            )
            live = {} if observed is None else observed.model_dump(exclude={"output_tokens"})
            if usage is not None and raw.get("parent_tool_use_id") is None:
                live["context_tokens"] = usage.input_tokens  # a subagent's call says nothing of this conversation
            if tool:
                return AgentEvent(kind="tool", tool=tool, tool_input=tool_input, tool_id=tool_id,
                                  session_id=sid, raw_type=kind, **live)
            if thinking and not any(t.strip() for t in texts):
                return AgentEvent(kind="thinking", text=_clip("".join(thinking)),
                                  session_id=sid, raw_type=kind, **live)
            return AgentEvent(kind="text", text="".join(texts), session_id=sid, raw_type=kind,
                              **live)

        if kind == "user":
            if raw.get("isReplay"):
                return None  # the echo of a message handed on stdin; its sender recorded the message itself
            # Tool results come back as a USER turn — that is the other half of a transcript.
            msg = raw.get("message") or {}
            # A typed prompt is saved as a plain string, a stream's turn as blocks.
            content = msg.get("content")
            for block in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
                if block.get("type") == "tool_result":
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                    # `is_error` is how Claude says the call failed; the Codex branches already
                    # carry it as `status`, and a failed call must not read as a success downstream.
                    return AgentEvent(kind="tool_result", text=_clip(str(body or "")),
                                      tool_id=str(block.get("tool_use_id") or ""),
                                      status="failed" if block.get("is_error") else "completed",
                                      session_id=sid, raw_type=kind)
                # Text on a `user` line is what was ASKED of the agent — the other half of the
                # conversation. Unnamed, an activity view shows only the agent talking. Claude
                # marks the turns IT wrote there (a Stop-hook block, an image note, a compaction
                # summary) `isSynthetic`: those are the harness, never the operator.
                if block.get("type") == "text":
                    text = str(block.get("text") or "")
                    said = "prompt" if not raw.get("isSynthetic") else (
                        "check" if text.startswith(_STOP_HOOK_FEEDBACK) else "injected")
                    return AgentEvent(kind=said, text=_clip(text, tail=_CLIP // 2 if said == "check" else 0),
                                      session_id=sid, raw_type=kind)
            return AgentEvent(kind="other", session_id=sid, raw_type=kind)

        if kind == "rate_limit_event":
            info = raw.get("rate_limit_info") or {}
            status = info.get("status", "?")
            which = info.get("rateLimitType", "")
            line = f"{which or 'rate'} limit: {status}"
            # A REFUSAL carries the instant its window reopens, and this line is the only thing a
            # downstream reader still has once the payload is gone — so the instant travels with
            # it, in ISO-8601, and the cooldown matches the window instead of a flat hour. Only on
            # a refusal: a healthy line's reset is for a window that is nowhere near exhausted,
            # and reading one of those is how a seven-day block gets cut back to an hour.
            reopens = info.get("resetsAt") if status == "rejected" else None
            if isinstance(reopens, (int, float)) and not isinstance(reopens, bool):
                line += f"; resets {datetime.fromtimestamp(reopens, UTC).isoformat()}"
            return AgentEvent(kind="rate_limit", session_id=sid, raw_type=kind, text=line, usage=self.usage(raw))

        if kind == "result":
            settled, cost = self._settle_result(raw, ledger)
            failed = bool(raw.get("is_error"))
            structured = raw.get("structured_output")
            has_final = structured is not None or bool(raw.get("result"))
            detail = json.dumps(structured) if structured is not None else str(
                raw.get("result") or raw.get("stop_reason") or raw.get("subtype") or ""
            )
            # The window this turn compacts below: the CLI's answer to its context request when this turn asked
            # (`control_response`, 2.1.288: the effective `autoCompactWindow`, capped at the model's own), else the
            # model's own (`modelUsage`) capped at the window this turn was launched with (2.1.288's rule). Both count
            # for this turn only: a later one (a resume with its message as an argument asks nothing) may run under
            # another setting.
            main = (raw.get("modelUsage") or {}).get(ledger.primary)
            model_window = main.get("contextWindow") if isinstance(main, dict) else None
            capped = min(model_window, ledger.compact_window) if isinstance(model_window, int) and ledger.compact_window else model_window
            window = ledger.window or capped
            ledger.window = ledger.compact_window = None
            # The turn's own thinking (`usage.output_tokens_details`: this turn, main thread; `modelUsage` is cumulative).
            details = (raw.get("usage") or {}).get("output_tokens_details") or {}
            thought = ledger.settle_thinking(details.get("thinking_tokens") if isinstance(details, dict) else None)
            return AgentEvent(
                kind="error" if failed else "done",
                session_id=sid, raw_type=kind,
                text=detail, cut=self.cut_of(detail, raw.get("api_error_status")) if failed else None,
                cost_usd=cost,
                final_text=has_final,
                **ContextGauge.reported({"context_window": window, "thinking_tokens": thought}),
                **({} if settled is None else settled.model_dump()),
            )

        return AgentEvent(kind="other", session_id=sid, raw_type=kind)

    def discover(self) -> list[dict]:
        """`claude agents --json` lists every live Claude session — including the user's own
        interactive windows, which galaius never spawned. That is what makes the supervisor a
        view of the whole machine rather than only of its own children."""
        if not self.available():
            return []
        try:
            out = subprocess.run(
                [self.executable(), "agents", "--json", "--all"],
                capture_output=True, text=True, timeout=20, check=True,
            ).stdout
            found = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            return []
        return found if isinstance(found, list) else []


class _CodexMeshServer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(min_length=1)
    args: list[str]
    env: dict[Literal["GALAIUS_PARENT_RUN_ID", GatewayServed.ROLE], Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class _CodexMesh(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    servers: dict[Literal["galaius"], _CodexMeshServer] = Field(alias="mcpServers", min_length=1)


#: Codex has no per-name flag for a NATIVE tool (its shell/file-edit/web-search/view-image
#: tools) — only for tools it reaches through an MCP SERVER (`mcp_servers.<id>.enabled_tools` /
#: `disabled_tools`, hosted config reference, verified 2026-09-21 against codex-rs 0.155.1's
#: `McpServerConfig`). galaius's own tools live behind exactly one such server ("galaius"), so
#: splitting a role's tool list on this prefix is what tells the EXACTLY-enforceable part from
#: the part codex can only approximate with a sandbox mode.
_GALAIUS_MCP_PREFIX = "mcp__galaius__"


def _partition_galaius_tools(tools) -> tuple[list[str], list[str]]:
    """(galaius MCP tool bare names, everything else) — see :data:`_GALAIUS_MCP_PREFIX`."""
    galaius_tools, other = [], []
    for tool in tools:
        if tool.startswith(_GALAIUS_MCP_PREFIX):
            galaius_tools.append(tool.removeprefix(_GALAIUS_MCP_PREFIX))
        else:
            other.append(tool)
    return galaius_tools, other


#: Reads are never gated on codex — confirmed live (2026-09-22, codex 0.155.1): under
#: `-s read-only` the agent still explores freely; only a WRITE fails. No sandbox or feature flag
#: speaks to these at all, so naming them is never a restriction codex needs to express.
_CODEX_ALWAYS_READABLE = frozenset({"Read", "Grep", "Glob"})
#: Claude Code's own subagent-orchestration primitives. Codex has no NATIVE tool by these names —
#: its own multi-agent feature is deliberately disabled here (`native_delegation_flags`, below),
#: so delegation on codex flows through galaius's OWN `mcp__galaius__*` mesh instead, already
#: enforced exactly by `_mcp_tool_scope_arguments`. Naming these is never a codex-side gap.
_CODEX_ORCHESTRATION_PROXY = frozenset({"Agent", "SendMessage", "ListAgents"})
#: Codex's file-write tool (`apply_patch`) is gated by `--sandbox`, never by its own name —
#: confirmed live: `read-only` rejects a write with "patch rejected: writing is blocked by
#: read-only sandbox"; `workspace-write` performs it. Model-catalog-driven, independent of the
#: shell tool (codex-rs 0.155.1 `tools/spec_plan.rs` L915-918) — it survives `shell_tool=false`.
_CODEX_WRITE_TOOLS = frozenset({"Write", "Edit"})
#: The shell tool can be REMOVED from the model's toolset entirely — `-c features.shell_tool=false`
#: (codex-rs 0.155.1 `tools/spec_plan.rs` L969-973) — not merely sandboxed. Confirmed live: under
#: `-s read-only` a shell WRITE (`echo x > f`) fails identically to apply_patch ("Read-only file
#: system") — the same OS-level sandbox governs both, so Bash-without-Write is exactly as safe as
#: an allow-list would have promised, never coarser.
_CODEX_SHELL_TOOLS = frozenset({"Bash"})
#: No native codex tool by this exact name; mapped onto local network access under the sandbox.
#: `sandbox_workspace_write.network_access` defaults false and is documented only under
#: workspace-write — read-only has no documented network toggle at all (unverified either way),
#: so WebFetch/WebSearch WITHOUT a write tool is the one combination with no clean codex mapping.
_CODEX_NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})
_CODEX_KNOWN_NATIVE = (_CODEX_ALWAYS_READABLE | _CODEX_ORCHESTRATION_PROXY | _CODEX_WRITE_TOOLS
                       | _CODEX_SHELL_TOOLS | _CODEX_NETWORK_TOOLS)


@dataclass(frozen=True)
class _CodexNativeMapping:
    """What a role's NATIVE (non-MCP) tool policy MEANS in codex's own vocabulary."""

    sandbox_mode: str
    shell_enabled: bool
    network_access: bool
    #: Tool names codex has literally no concept of — no sandbox, no feature flag, nothing.
    unmapped: tuple[str, ...] = ()
    #: The one combination with no documented codex equivalent (see `_CODEX_NETWORK_TOOLS`).
    network_without_write: bool = False

    @property
    def blocking_reason(self) -> str | None:
        """Why this policy cannot run on codex exactly as written, or None when it can — named
        CONCRETELY (the offending tool, or the specific combination), never "the native class"."""
        if self.unmapped:
            return (f"codex has no native tool, sandbox, or feature flag for "
                    f"{', '.join(self.unmapped)} at all — only its own MCP-server tools "
                    "(mcp__galaius__*) can be allow/deny-listed exactly")
        if self.network_without_write:
            return ("this role needs network (WebFetch/WebSearch) but no filesystem write — "
                    "codex documents a network toggle only under --sandbox workspace-write, "
                    "which would also allow writes this role's policy denies")
        return None


def _native_tool_sets(allowed_tools, denied_tools) -> tuple[frozenset[str], frozenset[str]]:
    """(allowed native tools, denied native tools) — the MCP-prefixed half is a separate,
    exactly-enforced concern (`_mcp_tool_scope_arguments`)."""
    _, allowed_other = _partition_galaius_tools(allowed_tools)
    _, denied_other = _partition_galaius_tools(denied_tools)
    return frozenset(allowed_other), frozenset(denied_other)


def _classify_native_tools(allowed: frozenset[str], denied: frozenset[str]) -> _CodexNativeMapping:
    """The role's native tool policy translated into codex's own sandbox + feature-flag
    vocabulary. An EMPTY allow-list means the policy never restricted this role at all — the same
    reading `Policy.tools_for` already uses — so codex starts from its fullest default toolset;
    an explicit denial then narrows ONLY the axis it names (denying Write must not also flip
    network off, so it is applied per-axis here, never by re-deriving from a "granted" set that
    would spuriously trip the network-without-write GAP nobody asked for).
    """
    if not allowed:
        return _CodexNativeMapping(
            sandbox_mode="read-only" if denied & _CODEX_WRITE_TOOLS else "workspace-write",
            shell_enabled=not bool(denied & _CODEX_SHELL_TOOLS),
            network_access=not bool(denied & _CODEX_NETWORK_TOOLS),
        )
    granted = allowed - denied
    unmapped = tuple(sorted(granted - _CODEX_KNOWN_NATIVE))
    needs_write = bool(granted & _CODEX_WRITE_TOOLS)
    needs_shell = bool(granted & _CODEX_SHELL_TOOLS)
    needs_network = bool(granted & _CODEX_NETWORK_TOOLS)
    return _CodexNativeMapping(
        sandbox_mode="workspace-write" if needs_write else "read-only",
        shell_enabled=needs_shell,
        # A shell that may write runs servers, test suites and the galaius launcher, all of which
        # bind loopback — and codex's sandbox has no loopback-only setting, only this one toggle.
        network_access=needs_write and (needs_network or needs_shell),
        unmapped=unmapped,
        network_without_write=needs_network and not needs_write,
    )


def _widen_for_coarse_acceptance(mapping: _CodexNativeMapping, coarse_accepted: bool) -> _CodexNativeMapping:
    """Once the operator accepts the EXACT tradeoff `blocking_reason` named (network needs
    workspace-write), widen to it — never any wider, never for the `unmapped` gap, which has no
    substitute to widen to at all."""
    if not (coarse_accepted and mapping.network_without_write):
        return mapping
    return replace(mapping, sandbox_mode="workspace-write", network_access=True, network_without_write=False)


class CodexProvider(AgentProvider):
    """OpenAI's Codex CLI (Apache-2.0), driven through its documented `codex exec` mode.

    Exercised end to end 2026-09-21 (codex 0.155.1): spawn, a three-turn resumed thread, and the
    quota refusal path. Its own MCP-server tools (`mcp__galaius__*`) map EXACTLY onto
    `mcp_servers.galaius.enabled_tools`/`disabled_tools` — no tradeoff, always applied.

    A role's NATIVE tool policy is READ, not refused wholesale: Write/Edit need
    `--sandbox workspace-write` (a write genuinely fails under `read-only`, both via apply_patch
    and via shell — verified live 2026-09-22); Bash needs the shell tool kept, else
    `-c features.shell_tool=false` removes it from the model's toolset entirely (verified against
    codex-rs 0.155.1 source); Read/Grep/Glob need nothing (reads are never gated); Agent/
    SendMessage/ListAgents have no codex-native tool by that name at all and are never a gap
    (delegation instead flows through galaius's own exactly-enforced MCP mesh). The one
    remaining gap: WebFetch/WebSearch WITHOUT a write tool, since codex documents a network
    toggle only under workspace-write — refused, naming the combination, unless the operator
    accepts the coarser (workspace-write) substitute (`Policy.accepts_coarse_tool_policy`).
    """

    name = "codex"
    #: codex-cli 0.155.1 `exec --help`: « If not provided as an argument (or if `-` is used), instructions are read from
    #: stdin »; `exec resume --help`: « If `-` is used, read from stdin ».
    stdin_prompt = "-"
    native_providers = frozenset({"openai", "chatgpt"})
    binary = "codex"
    installers = {
        "posix": ProgramInstaller(url="https://chatgpt.com/codex/install.sh", hosts=OPENAI_INSTALL_HOSTS, shell=("sh",), bin_dirs=("~/.local/bin",),
                                  environment={"CODEX_NON_INTERACTIVE": "1"}),
        "windows": ProgramInstaller(url="https://chatgpt.com/codex/install.ps1", hosts=OPENAI_INSTALL_HOSTS, shell=POWERSHELL_FILE,
                                    bin_dirs=("%LOCALAPPDATA%/Programs/OpenAI/Codex/bin",)),
    }
    #: Prints its vendor's page and a one-time code the person types there, on any browser.
    login_arguments = ("login", "--device-auth")
    logout_arguments = ("logout",)
    auth_home_env = ("CODEX_HOME",)
    home_folder = ".codex"
    settings_file = "hooks.json"
    transcript_glob = "sessions/**/rollout-*.jsonl"
    #: Text Codex itself puts in a user turn (its environment and instructions), never the person's.
    _HARNESS_TURN = ("<", "# AGENTS.md instructions")

    def login_keys(self, environment: Mapping[str, str]) -> frozenset[str]:
        """Its API-key variables only while its login runs on a key: signed in with ChatGPT
        (`auth.json` `auth_mode` chatgpt) it reads none of them."""
        try:
            mode = json.loads((self.auth_home(environment) / "auth.json").read_text()).get("auth_mode")
        except (OSError, ValueError, AttributeError):
            mode = None
        return frozenset() if mode == "chatgpt" else super().login_keys(environment)

    def transcript_id(self, path: Path) -> str:
        """`rollout-<started>-<thread id>.jsonl`: the thread id is its last 36 characters."""
        return path.stem[-36:]

    def transcript_event(self, raw: dict) -> AgentEvent | None:
        """A session record's `response_item`: what was asked, what it answered, each tool call.
        Its `event_msg` records repeat the same turns and are not read."""
        payload = raw.get("payload")
        if raw.get("type") != "response_item" or not isinstance(payload, dict):
            return None
        at = instant(raw.get("timestamp"))
        match payload.get("type"), payload.get("role"):
            case "message", "user" | "assistant" as role:
                text = "".join(str(block.get("text") or "") for block in payload.get("content") or () if isinstance(block, dict)).strip()
                if not text or (role == "user" and text.startswith(self._HARNESS_TURN)):
                    return None
                return AgentEvent(kind="prompt" if role == "user" else "text", text=text, at=at)
            case "function_call" | "custom_tool_call", _:
                given = payload.get("arguments", payload.get("input"))
                with suppress(ValueError, TypeError):
                    given = json.loads(given)
                return AgentEvent(kind="tool", tool=str(payload.get("name") or "tool"), tool_input=_summarise_input(given), at=at)
        return None

    def transcript_place(self, raw: dict) -> tuple[str, str]:
        payload = raw.get("payload")
        return (str(payload.get("cwd") or ""), "") if raw.get("type") == "session_meta" and isinstance(payload, dict) else ("", "")

    def transcript_branch(self, raw: dict) -> str:
        payload = raw.get("payload")
        git = payload.get("git") if raw.get("type") == "session_meta" and isinstance(payload, dict) else None
        return str(git.get("branch") or "") if isinstance(git, dict) else ""

    #: Its session `originator` values (as its CLI names them) for the terminal and the editor extension
    #: (`codex_exec`: headless).
    #: Its session record's `source`: its terminal UI, its editor extension. Never `exec` (headless,
    #: an agent's run) or a sub-agent's thread (`{"subagent": ...}`: a delegate, an approval review),
    #: whatever program (`originator`) wrote them.
    typed_origins = frozenset({"cli", "vscode"})

    def transcript_origin(self, raw: dict) -> str:
        """Its session record's `source`; a sub-agent thread's (`{"subagent": {...}}`) by its one key."""
        payload = raw.get("payload")
        match payload.get("source") if raw.get("type") == "session_meta" and isinstance(payload, dict) else None:
            case str() as source:
                return source
            case dict() as source:
                return next(iter(source), "")
            case _:
                return ""

    def usage(self, line: dict) -> tuple[UsageWindow, ...]:
        """`rate_limits` (`primary` / `secondary`: used_percent, window_minutes, resets_at epoch s),
        top-level or in the line's `payload` (a session record's `token_count` line)."""
        payload = line.get("payload")
        limits = line.get("rate_limits") or (payload.get("rate_limits") if isinstance(payload, dict) else None)
        if not isinstance(limits, dict):
            return ()
        now = datetime.now(UTC)
        return tuple(
            UsageWindow(window=UsageWindow.nearest(window["window_minutes"]), used_percent=float(window["used_percent"]), observed_at=now,
                        resets_at=datetime.fromtimestamp(window["resets_at"], UTC) if isinstance(window.get("resets_at"), (int, float)) else None)
            for window in (limits.get("primary"), limits.get("secondary"))
            if isinstance(window, dict) and isinstance(window.get("used_percent"), (int, float)) and isinstance(window.get("window_minutes"), (int, float))
        )
    can_resume = True

    def session_files(self, home: Path, cwd: Path, session: str) -> tuple[Path, ...]:
        """Codex's rollout of thread `session` (`sessions/<y>/<m>/<d>/rollout-*-<thread>.jsonl`), wherever it was started."""
        return tuple(sorted((home / "sessions").rglob(f"rollout-*-{session}.jsonl"))) if (home / "sessions").is_dir() else ()
    can_queue = True
    verified = True
    caveat = ("Read/Grep/Glob/Write/Edit/Bash map exactly onto --sandbox + feature flags; "
              "WebFetch/WebSearch without a write tool has no codex-documented equivalent and "
              "needs coarse_tool_policy acceptance; Codex has no per-tool flag for native tools")
    permission_option = "--sandbox"
    can_attach_images = True
    # Native children bypass Galaius's role/model policy when Codex hooks are untrusted.
    # Delegates must return through the common launcher, including resumed conversations.
    native_delegation_flags = (
        "-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false",
    )
    #: `codex exec` refuses a folder that is neither a Git repository nor marked trusted in
    #: `~/.codex/config.toml`. Galaius already decided where the agent runs (the folder the
    #: computer allows, under the sandbox above); a user never edits codex's trust list for it.
    exec_flags = ("--skip-git-repo-check",)
    #: A fixed, galaius-owned provider id for a routed OpenAI-wire endpoint — never derived from
    #: model text, never the built-in "openai"/"chatgpt" ids (those are tied to the user's own
    #: ChatGPT session or real OpenAI key; reusing them here would risk that credential reaching a
    #: third-party box or HF's router instead of OpenAI itself).
    _OPENAI_COMPAT_PROVIDER_ID = "galaius_openai_compat"
    #: Codex's own Windows sandbox, set by galaius on every start on Windows unless the owner's
    #: `~/.codex/config.toml` names one (e.g. "elevated", which needs a one-time administrator
    #: setup). Unset, Codex has no OS sandbox there and refuses commands and edits instead;
    #: "unelevated" (a restricted token + folder ACLs) needs no administrator and no prompt.
    WINDOWS_SANDBOX: ClassVar[str] = "unelevated"

    def _bwrap_refusal(self) -> str | None:
        """On Linux every Codex command (shell, apply_patch, read-only too) runs in bubblewrap: the
        first `bwrap` on PATH, else the one bundled with the CLI. Its Landlock fallback refuses
        workspace-write since 0.155 (`.git` / `.codex` / `.agents` stay read-only, which Landlock
        cannot express), so a refused user namespace leaves it no sandbox at all."""
        if not sys.platform.startswith("linux") or not self.available():
            return None
        bundled = sorted(Path(self.executable()).parent.parent.glob("node_modules/@openai/codex-linux-*/vendor/*/codex-resources/bwrap"))
        bwrap = shutil.which("bwrap") or (str(bundled[0]) if bundled else None)
        return None if bwrap is None else userns_refusal(bwrap)

    def sandbox_refusal(self) -> str | None:
        """Refused only when neither Codex's sandbox nor Galaius's own confinement can start here."""
        if (refused := self._bwrap_refusal()) is None or (unconfinable := confinement_refusal()) is None:
            return None
        return f"{refused}; Galaius cannot confine it itself either: {unconfinable}"

    def _confinement(self, mode: str, mcp_config: str | None) -> Confinement | None:
        """Where Codex's sandbox cannot start, Galaius's own (`Confinement`) in its place, writing in
        the start folder only for `workspace-write`; None where Codex's starts or none is asked."""
        if mode == "danger-full-access" or self._bwrap_refusal() is None or confinement_refusal() is not None:
            return None
        server = _CodexMesh.model_validate_json(mcp_config).servers["galaius"] if mcp_config is not None else None
        return Confinement(
            write_start=mode == "workspace-write", home_variable="CODEX_HOME", default_home=".codex",
            shared=("AGENTS.md", "AGENTS.override.md", "prompts", "rules", "skills", "plugins", "hooks.json"),
            copied=("auth.json", "config.toml", "installation_id"), sessions=("sessions",), signin=SignIn(file="auth.json", account=("tokens", "account_id")),
            relayed=RelayedServer(argv=(server.command, *server.args), env=dict(server.env), key="mcp_servers.galaius.args=", switch=("mcp_servers", "galaius"))
            if server is not None else None,
        )

    def _started(self, argv: list[str], confinement: Confinement | None) -> list[str]:
        """`argv` as it starts: inside `confinement` (its program by path: the confinement starts it), else unchanged."""
        return argv if confinement is None else confinement.command([self.executable(), *argv[1:]])

    @classmethod
    def platform_flags(cls) -> tuple[str, ...]:
        """What every Codex start on this system adds: on Windows, the sandbox it runs in."""
        if sys.platform != "win32":
            return ()
        config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
        try:
            chosen = tomllib.loads(config.read_text(encoding="utf-8")).get("windows", {}).get("sandbox")
        except (OSError, tomllib.TOMLDecodeError):
            chosen = None
        return () if chosen else ("-c", f'windows.sandbox="{cls.WINDOWS_SANDBOX}"')

    def can_run(self, model: Model, env: dict[str, str]) -> bool:
        if super().can_run(model, env):
            return True
        # Codex speaks the OpenAI chat-completions wire protocol natively; an operator-configured
        # endpoint galaius knows (hf, vllm) is reachable too, routed through a NAMED provider —
        # see `_openai_compat_arguments`. Checked by KEY: overlay_for also returns a non-empty,
        # ANTHROPIC-shaped dict for ollama, which this binary cannot speak at all.
        #
        # No `model.is_available()` check here, unlike ClaudeCodeProvider's ollama branch: that
        # method proves availability from a DECLARED catalog key or a live daemon answering
        # (`_served`) — neither exists yet for hf/vllm (no discovery pass merges what a router or
        # self-hosted box actually serves into the catalog, the way `Model.merge_ollama` does for
        # Ollama). Requiring it would make an EXPLICITLY-configured `VLLM_BASE_URL` unroutable by
        # ANY criterion forever. The operator having set the base URL at all is itself the local,
        # provable fact standing in — same reasoning `Model.key_missing` already uses for a
        # provider "we've never heard of": absence of catalog proof must never override an
        # explicit route (see that method's docstring). A follow-on discovery pass (`/v1/models`
        # against the router/box, mirroring `merge_ollama`) would let a REAL `is_available()`
        # replace this once it exists.
        return "OPENAI_BASE_URL" in overlay_for(f"{model.provider}/{model.id}", env)

    def _openai_compat_arguments(self, base_url: str | None) -> list[str]:
        """``-c`` flags that stand up a NEW, isolated ``model_providers`` entry at ``base_url``
        and select it — never touching the built-in ``openai``/``chatgpt`` provider ids. Empty
        when no base URL was routed (the common case: a native ChatGPT/API-key run).

        ``env_key`` always names the same fixed ``GALAIUS_OPENAI_COMPAT_KEY`` — never
        ``OPENAI_API_KEY`` — so this endpoint can only ever see the token `profiles.py` copied
        there for IT specifically (``_KEY_FROM``), never a real OpenAI/ChatGPT credential sitting
        in the same process env for something else. Codex tolerates the var being unset (no
        Authorization header sent) — correct for a bare self-hosted server with no ``--api-key``.
        """
        if not base_url:
            return []
        prefix = f"model_providers.{self._OPENAI_COMPAT_PROVIDER_ID}"
        return [
            "-c", f"{prefix}.name={json.dumps('galaius-openai-compat')}",
            "-c", f"{prefix}.base_url={json.dumps(base_url)}",
            # Verified live 2026-09-22 against the installed codex-rs 0.155.1: `wire_api = "chat"`
            # is REFUSED at config-load time ("no longer supported... set wire_api = 'responses'",
            # https://github.com/openai/codex/discussions/7782) — a version older than this one may
            # have accepted "chat"; this binary does not. HF's router and vLLM both also serve
            # `/v1/responses` (research store, 2026-09-22), so this is not a narrowing of what it
            # can reach.
            "-c", f"{prefix}.wire_api={json.dumps('responses')}",
            "-c", f"{prefix}.env_key={json.dumps(COMPAT_KEY)}",
            "-c", f"model_provider={json.dumps(self._OPENAI_COMPAT_PROVIDER_ID)}",
        ]

    def permission_modes(self) -> list[PermissionMode]:
        """Shared permission intents translated to Codex's two sandbox scopes.

        ``bypassPermissions`` has no honest Codex equivalent: Codex has no unsandboxed mode. It
        maps to the safest useful substitute, ``workspace-write``, and remains marked unrestricted
        so tool callers cannot request it through model-produced text.
        """
        return [
            PermissionMode("plan", "Plan only", "reads files without changing them", provider_mode="read-only", touch=TouchScope.READ_ONLY, approval=ApprovalIntent.NEVER),
            PermissionMode("manual", "Ask every time", "uses Codex's read-only sandbox", provider_mode="read-only", touch=TouchScope.READ_ONLY, approval=ApprovalIntent.ASK),
            PermissionMode("auto", "Ask when it matters", "changes files inside the assigned workspace", provider_mode="workspace-write", touch=TouchScope.WORKSPACE_WRITE, approval=ApprovalIntent.AUTO_SAFE),
            PermissionMode("acceptEdits", "May edit files", "changes files inside the assigned workspace", provider_mode="workspace-write", touch=TouchScope.WORKSPACE_WRITE, approval=ApprovalIntent.AUTO_EDITS),
            PermissionMode("dontAsk", "Stop asking", "changes files inside the assigned workspace", provider_mode="workspace-write", touch=TouchScope.WORKSPACE_WRITE, approval=ApprovalIntent.NEVER),
            PermissionMode(
                "bypassPermissions", "No restrictions",
                "no exact Codex equivalent; safely confined to the workspace-write sandbox",
                unrestricted=True, provider_mode="workspace-write", touch=TouchScope.FULL_ACCESS, approval=ApprovalIntent.NEVER,
            ),
        ]

    def app_server_command(self) -> list[str]:
        """The installed local-session protocol entry point, resolved before spawning."""
        return [self.executable(), *self.platform_flags(), "app-server", "--listen", "stdio://"]

    #: Its app-server's refusal when no account is signed in (0.155.1).
    SIGNED_OUT: ClassVar[re.Pattern[str]] = re.compile(r"authentication required", re.IGNORECASE)

    def app_server_ask(self, method: str, params: dict) -> tuple[list[str], tuple[dict, ...]]:
        """One request (id 2) on its app-server, after its handshake (id 1)."""
        return (self.app_server_command(),
                ({"id": 1, "method": "initialize", "params": self.initialize_params()}, {"method": "initialized", "params": {}},
                 {"id": 2, "method": method, "params": params}))

    def usage_command(self) -> tuple[list[str], tuple[dict, ...]]:
        """`account/rateLimits/read` on its app-server (0.155.1): the login's windows as ChatGPT has them."""
        return self.app_server_ask("account/rateLimits/read", {})

    def account_command(self) -> tuple[list[str], tuple[dict, ...]]:
        """`account/read` on its app-server (0.155.1, probed 2026-10-30: `{account: {type, email, planType}}`): its status
        command says only « Logged in using ChatGPT », the same for every ChatGPT account."""
        return self.app_server_ask("account/read", {"refreshToken": False})

    #: Its `account/read` answer's, never `planType`.
    IDENTITY_KEYS = ("type", "email")

    def account_answer(self, line: dict) -> tuple[str, str] | None:
        """Its answer to request 2; an error (a CLI without `account/read`), no account, or one naming no e-mail (an API
        key, which only the status line tells apart) names none."""
        if line.get("id") != 2:
            return None
        result = line.get("result")
        account = result.get("account") if isinstance(result, dict) else None
        if not isinstance(account, dict) or not account.get("email"):
            return "", ""
        return str(account["email"]), self.identity_of(account)

    @staticmethod
    def initialize_params() -> dict:
        """What galaius says of itself opening its app-server (every session, the usage ask)."""
        return {"clientInfo": {"name": "galaius", "title": "Galaius", "version": installed_version()}, "capabilities": {"experimentalApi": False}}

    def usage_answer(self, line: dict) -> tuple[UsageWindow, ...] | None:
        """Its answer to request 2: `result.rateLimits` `primary` / `secondary` {usedPercent, windowDurationMins, resetsAt
        epoch s}; signed out (« codex account authentication required … »): none; any other error says nothing."""
        if line.get("id") != 2:
            return None
        error = line.get("error")
        if isinstance(error, dict):
            return () if self.SIGNED_OUT.search(str(error.get("message", ""))) else None
        result = line.get("result")
        limits = result.get("rateLimits") if isinstance(result, dict) else None
        if not isinstance(limits, dict):
            return None
        return self.usage({"rate_limits": {name: {"used_percent": window.get("usedPercent"), "window_minutes": window.get("windowDurationMins"), "resets_at": window.get("resetsAt")}
                                           for name in ("primary", "secondary") if isinstance(window := limits.get(name), dict)}})

    def auth_command(self) -> list[str]:
        return [self.executable(), "login", "status"]

    def accepts_subscription_auth(self, stdout: str, stderr: str) -> bool:
        lines = [line.strip().lower() for line in f"{stdout}\n{stderr}".splitlines()]
        return any("logged in using chatgpt" in line and "api key" not in line for line in lines)

    def accepts_any_auth(self, stdout: str, stderr: str) -> bool:
        return bool(self.login_identity(stdout, stderr))

    def login_identity(self, stdout: str, stderr: str) -> str:
        """Codex says « Logged in using ChatGPT » (or an API key) on stderr, and nothing else names the login."""
        return "\n".join(line.strip() for line in f"{stdout}\n{stderr}".splitlines()
                         if line.strip().lower().startswith("logged in using "))

    @staticmethod
    def mesh_arguments(mcp_config: str | None, relayed: bool = False) -> list[str]:
        """Project the launcher's credential-free stdio mesh without granting access. `relayed`: Codex
        starts `RELAY` instead, to the server its `Confinement` runs outside."""
        if mcp_config is None:
            return []
        try:
            server = _CodexMesh.model_validate_json(mcp_config).servers["galaius"]
        except ValueError:
            # Validation errors include input values; never echo rejected environment data.
            raise ValueError("Invalid credential-free Codex mesh configuration") from None
        command, args = (sys.executable, ["-I", "-c", RELAY, RELAY_PORT]) if relayed else (server.command, server.args)
        return [
            "-c", f"mcp_servers.galaius.command={json.dumps(command, ensure_ascii=False)}",
            "-c", f"mcp_servers.galaius.args={json.dumps(args, ensure_ascii=False)}",
            # Its tools are the role's (enabled_tools / disabled_tools); a run has no one to answer an
            # approval prompt, and under approval_policy=never Codex 0.155 refuses every tool call
            # without a read-only annotation instead (codex-rs config/src/mcp_types.rs).
            "-c", 'mcp_servers.galaius.default_tools_approval_mode="approve"',
            *(item for key, value in sorted(server.env.items()) for item in ("-c", f"mcp_servers.galaius.env.{key}=" + json.dumps(value, ensure_ascii=False))),
        ]

    def validate_tool_policy(self, allowed_tools: list[str] | None, denied_tools: tuple[str, ...], *,
                             coarse_accepted: bool = False) -> None:
        validate_denied_tools(denied_tools)
        if no_tools(allowed_tools):
            # Its shell and its file reading cannot both be switched off: no sandbox or flag runs it with no tool.
            raise UnsupportedToolPolicy("Codex cannot run an agent with no tools (its shell and file reading cannot be switched off)")
        allowed_native, denied_native = _native_tool_sets(allowed_tools or [], denied_tools)
        reason = _classify_native_tools(allowed_native, denied_native).blocking_reason
        if reason and not coarse_accepted:
            raise UnsupportedToolPolicy(
                f"{reason}. To run this role on codex anyway under its coarser workspace-write "
                'sandbox, accept the tradeoff: add "coarse_tool_policy": {"<role>": ["codex"]} to '
                "~/.galaius/agents.json."
            )

    def _native_sandbox_arguments(
        self, allowed_tools: list[str], denied_tools: tuple[str, ...], *,
        permission_mode: str | None, coarse_accepted: bool = False,
    ) -> list[str]:
        """``--sandbox`` derived from the role's OWN tool policy — never left unset (codex
        defaults an unset sandbox to read-only in `exec` mode, which once silently ran every
        builder role read-only) — plus the feature/network flags that same policy implies.

        An explicit ``permission_mode`` is the operator's own choice (same precedent as Claude's
        plan/manual modes) and overrides only the read-vs-write AXIS; the shell and network flags
        stay policy-derived regardless, since no `PermissionMode` speaks to those. ``coarse_accepted``
        widens ONLY the named network-without-write tradeoff `validate_tool_policy` already let through.
        """
        native_permission_mode = self.provider_permission_mode(permission_mode)
        mapping = _widen_for_coarse_acceptance(
            _classify_native_tools(*_native_tool_sets(allowed_tools, denied_tools)), coarse_accepted,
        )
        mode = native_permission_mode if native_permission_mode is not None else mapping.sandbox_mode
        args = [self.permission_option, mode]
        if not mapping.shell_enabled:
            args += ["--disable", "shell_tool"]
        if mapping.network_access:
            args += ["-c", "sandbox_workspace_write.network_access=true"]
        approval = self.provider_approval_mode(permission_mode)
        if approval is not None:
            if approval == "approve-for-me":
                args += ["--approve-for-me"]
            else:
                raise UnsupportedToolPolicy(f"Codex approval intent has no documented CLI mapping: {permission_mode!r}")
        return args

    @staticmethod
    def _mcp_tool_scope_arguments(
        allowed_tools: list[str], denied_tools: tuple[str, ...],
    ) -> list[str]:
        """``-c mcp_servers.galaius.enabled_tools=[...]``/``disabled_tools=[...]`` — the part of
        a role's tool restriction codex enforces EXACTLY, applied whatever the coarse-acceptance
        verdict (never widened by it, never gated on it)."""
        allowed_galaius, _ = _partition_galaius_tools(allowed_tools)
        denied_galaius, _ = _partition_galaius_tools(denied_tools)
        args = []
        if allowed_galaius:
            args += ["-c", f"mcp_servers.galaius.enabled_tools={json.dumps(sorted(set(allowed_galaius)))}"]
        if denied_galaius:
            args += ["-c", f"mcp_servers.galaius.disabled_tools={json.dumps(sorted(set(denied_galaius)))}"]
        return args

    def command(self, task: str, *, cwd: str, model: str | None, mcp_config: str | None,
                run_id: str, agent: str | None = None,
                permission_mode: str | None = None,
                allowed_tools: list[str] | None = None,
                reasoning: str | None = None,
                agent_prompt: str | None = None, denied_tools: tuple[str, ...] = (),
                image_paths: tuple[Path, ...] = (), coarse_accepted: bool = False,
                base_url: str | None = None, environment: Mapping[str, str] | None = None) -> list[str]:
        self.validate_tool_policy(allowed_tools, denied_tools, coarse_accepted=coarse_accepted)
        task = self._inject_definition(agent, task, agent_prompt)
        sandbox = self._native_sandbox_arguments(allowed_tools or [], denied_tools, permission_mode=permission_mode, coarse_accepted=coarse_accepted)
        confinement = self._confinement(sandbox[1], mcp_config)
        argv = [self.binary, "exec", "--json", *self.exec_flags, *self.native_delegation_flags, *self.platform_flags(), *self._setting_sources(environment)]
        argv += self.mesh_arguments(mcp_config, relayed=confinement is not None)
        argv += self._mcp_tool_scope_arguments(allowed_tools or [], denied_tools)
        argv += sandbox if confinement is None else [self.permission_option, "danger-full-access", *sandbox[2:]]
        argv += self._openai_compat_arguments(base_url)
        if model:
            argv += ["--model", model]
        if reasoning is not None:
            argv += ["-c", f'model_reasoning_effort="{self.provider_thinking_level(reasoning)}"']
        # The task goes LAST, after "--": one starting with "-" stays the task ("-" alone would
        # otherwise mean "read it from stdin").
        return self._started([*argv, *self._image_args(image_paths), "--", task], confinement)

    def image_attachment_support(self) -> bool:
        """Verify ``--image`` against the installed ``codex exec --help`` output."""
        if not self.can_attach_images:
            return False
        try:
            completed = subprocess.run(
                [self.executable(), "exec", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and "--image" in (completed.stdout or "")

    def resume_command(
        self, session_id: str, message: str, *, model: str | None = None,
        permission_mode: str | None = None, reasoning: str | None = None,
        agent: str | None = None,
        mcp_config: str | None = None, allowed_tools: list[str] | None = None,
        agent_prompt: str | None = None, denied_tools: tuple[str, ...] = (),
        coarse_accepted: bool = False, base_url: str | None = None, fork_to: str | None = None,
    ) -> list[str]:
        """Resume a stopped session with fresh model and reasoning policy.

        ``exec resume`` has no ``--sandbox`` option: every turn names its scope with ``-c sandbox_mode``,
        from the same policy as its first turn, never the scope the session saved (a turn begun inside
        Galaius's confinement saved danger-full-access). Callers validate the recorded permission.

        Unlike Claude Code — whose ``--agents`` JSON carries the role definition on a channel
        separate from the resumed message, so it is cheap to resend every turn — Codex has no such
        channel: :meth:`command` can only seed a role by baking its definition into the FIRST
        user turn. That turn is already part of the saved session; re-baking the whole definition
        into every resumed message doubles input tokens per turn and was observed to make the
        model re-answer the ORIGINAL task verbatim instead of the new message (live: turn 2 of a
        resumed run echoed turn 1's reply byte-for-byte until this injection was dropped; a bare
        follow-up on the same session answered correctly). A resume sends exactly the new message.
        """
        if fork_to is not None:
            raise ValueError("codex continues its own session; it cannot resume into a copy")
        self.validate_tool_policy(allowed_tools, denied_tools, coarse_accepted=coarse_accepted)
        if agent:
            self.validate_agent_name(agent)
        mapping = _widen_for_coarse_acceptance(
            _classify_native_tools(*_native_tool_sets(allowed_tools or [], denied_tools)), coarse_accepted,
        )
        native = self.provider_permission_mode(permission_mode) if permission_mode is not None else None
        confinement = self._confinement(native if native is not None else mapping.sandbox_mode, mcp_config)
        argv = [self.binary, "exec", "resume", "--json", *self.exec_flags, *self.native_delegation_flags, *self.platform_flags(), *self._setting_sources()]
        argv += self.mesh_arguments(mcp_config, relayed=confinement is not None)
        argv += self._mcp_tool_scope_arguments(allowed_tools or [], denied_tools)
        argv += self._openai_compat_arguments(base_url)
        # An explicit `permission_mode` is the operator's own per-turn choice; the shell/network
        # flags are config, not session state, resent every turn from the SAME policy, never widened.
        argv += ["-c", f'sandbox_mode="{"danger-full-access" if confinement is not None else native or mapping.sandbox_mode}"']
        if permission_mode is not None:
            approval = self.provider_approval_mode(permission_mode)
            if approval is not None:
                if approval == "approve-for-me":
                    argv += ["--approve-for-me"]
                else:
                    raise UnsupportedToolPolicy(f"Codex approval intent has no documented CLI mapping: {permission_mode!r}")
        if not mapping.shell_enabled:
            argv += ["-c", "features.shell_tool=false"]
        if mapping.network_access:
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if model:
            argv += ["--model", model]
        if reasoning is not None:
            argv += ["-c", f'model_reasoning_effort="{self.provider_thinking_level(reasoning)}"']
        return self._started([*argv, "--", session_id, message], confinement)

    def user_instructions(self, environment: Mapping[str, str]) -> Path | None:
        home = self.auth_home(environment)
        # Codex reads AGENTS.override.md INSTEAD of AGENTS.md when both exist.
        return None if (home / "AGENTS.override.md").exists() else home / "AGENTS.md"

    def _inject_definition(self, agent: str | None, task: str, agent_prompt: str | None = None) -> str:
        if not agent:
            if agent_prompt is not None:
                raise ValueError("A named role is required for a pinned prompt")
            return task
        self.validate_agent_name(agent)
        if agent_prompt is None:
            catalog = AgentCatalog.active()
            if catalog is not None:
                return catalog.definition(agent, task)
            agent_prompt = self.definition_prompt(agent)
            agent_prompt = f"AGENT_ROLE: {agent}\n\n{agent_prompt}"
        return f"{agent_prompt}\n\nDelegated task:\n{task}"

    def definition_path(self, agent: str) -> Path | None:
        catalog = AgentCatalog.active()
        if catalog is not None:
            return catalog.definition_path(agent)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", agent):
            return None
        root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "galaius/prompts"
        paths = [root / "agents" / f"{agent}.md", *root.glob(f"scopes/*/agents/{agent}.md")]
        found = [path for path in paths if path.is_file()]
        return found[0] if len(found) == 1 else None

    def agent_definitions(self) -> list[str]:
        catalog = AgentCatalog.active()
        if catalog is not None:
            return sorted(value.role_key for value in catalog.snapshot.agents if value.role_key is not None)
        root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "galaius/prompts/agents"
        return sorted(path.stem for path in root.glob("*.md"))

    def parse(self, line: str, ledger: UsageLedger | None = None) -> AgentEvent | None:
        """`_parse_line`'s event, carrying the usage windows the line reports (`usage`): a line saying
        nothing else is a `rate_limit` event of its own."""
        event = self._parse_line(line, ledger)
        if event is None or "rate_limits" not in line or not (usage := self.usage(json.loads(line))):
            return event
        said = ", ".join(f"{window.window} {window.used_percent:g}%" for window in usage)
        return event.model_copy(update={"usage": usage, **({"kind": "rate_limit", "text": f"usage: {said}"} if event.kind == "other" else {})})

    def _parse_line(self, line: str, ledger: UsageLedger | None = None) -> AgentEvent | None:
        line = line.strip()
        if not line:
            return None
        try:
            raw = json.loads(line)
        except ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        ledger = UsageLedger() if ledger is None else ledger
        kind = str(raw.get("type", ""))
        session_id = raw.get("thread_id")
        turn_id = raw.get("turn_id")
        if kind == "thread.started":
            return AgentEvent(kind="started", session_id=session_id, raw_type=kind)
        if kind == "turn.started":
            return AgentEvent(kind="started", session_id=session_id, turn_id=turn_id, raw_type=kind)
        if kind in ("turn.failed", "error"):
            error = raw.get("error") or {}
            text = error.get("message", "") if isinstance(error, dict) else str(error)
            return AgentEvent(kind="error", session_id=session_id, turn_id=turn_id,
                              raw_type=kind, text=_clip(text or line), cut=self.cut_of(text))
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = raw.get("item") or {}
            item_type = item.get("type") if isinstance(item, dict) else None
            completed = kind == "item.completed"
            if item_type == "agent_message" and completed:
                return AgentEvent(kind="text", session_id=session_id, turn_id=turn_id,
                                  raw_type=kind, text=str(item.get("text") or ""))
            if item_type == "command_execution":
                failed = item.get("status") == "failed" or bool(item.get("exit_code"))
                return AgentEvent(
                    kind="tool_result" if completed else "tool",
                    raw_type=kind,
                    session_id=session_id, turn_id=turn_id,
                    tool="Shell", tool_id=str(item.get("id") or ""),
                    tool_input=_clip(str(item.get("command") or "")),
                    status=("failed" if failed else "completed") if completed else "running",
                    text=_clip(str(item.get("aggregated_output") or item.get("status") or "")),
                )
            if item_type == "file_change":
                changes = item.get("changes") or []
                detail = ", ".join(
                    f"{change.get('kind', 'change')} {change.get('path', '')}"
                    for change in changes if isinstance(change, dict)
                )
                return AgentEvent(
                    kind="tool_result" if completed else "tool", session_id=session_id,
                    turn_id=turn_id, raw_type=kind,
                    tool="Edit files", tool_id=str(item.get("id") or ""),
                    tool_input=_clip(detail), text=_clip(detail) if completed else "",
                    status=("failed" if item.get("status") == "failed" else "completed") if completed else "running",
                )
        if kind == "turn.completed":
            # `usage` is the THREAD's cumulative total — a resumed `exec` reports every earlier
            # turn again (monotonic in all 615 recorded turns) — in OpenAI's meaning: cached and
            # cache-write tokens inside `input_tokens`, reasoning inside `output_tokens`.
            usage = raw.get("usage")
            settled = None
            if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
                settled, _ = ledger.settle({"": TokenUsage(**{
                    field: value for field in TOKEN_FIELDS
                    if isinstance(value := usage.get(field), int)
                })}, None)
            return AgentEvent(
                kind="done",
                session_id=session_id, turn_id=turn_id,
                raw_type=kind,
                **ContextGauge.reported({"thinking_tokens": ledger.reasoned(usage.get("reasoning_output_tokens")) if isinstance(usage, dict) else None}),
                **({} if settled is None else settled.model_dump()),
            )
        return AgentEvent(kind="other", raw_type=kind, text=_clip(line, 200))


class _MediaSessionProvider(Protocol):
    name: str
    binary: str
    native_providers: frozenset[str]
    media_model_field: str
    no_extra_usage_guidance: str

    def available(self) -> bool: ...
    def executable(self) -> str: ...
    def model_id_for(self, model: Model) -> str: ...
    def can_run(self, model: Model, env: dict[str, str]) -> bool: ...
    def supports_session_media(self, media_kind: str) -> bool: ...
    def subscription_env(
        self, base: dict[str, str] | None = None, *, temp_dir: Path | None = None
    ) -> dict[str, str]: ...
    async def subscription_authenticated(
        self, env: dict[str, str], *, timeout: float = 10
    ) -> bool: ...
    async def media_isolation_args(
        self, env: dict[str, str], *, cwd: Path, timeout: float
    ) -> tuple[str, ...]: ...
    async def media_cli_version(
        self, env: dict[str, str], *, cwd: Path, timeout: float
    ) -> str: ...
    def media_command(
        self, *, cwd: Path, model: str | None, media_paths: list[Path],
        schema_path: Path | None, schema_json: str | None, mcp_config: Path,
        settings_path: Path, isolation_args: tuple[str, ...] = (),
    ) -> list[str]: ...
    async def run_media_process(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
        stdin: bytes | None = None,
    ) -> list[AgentEvent]: ...
    def media_result(self, events: list[AgentEvent]) -> _MediaResult: ...


_claude = ClaudeCodeProvider()
PROVIDERS: dict[str, AgentProvider] = {p.name: p for p in (_claude, CodexProvider())}
MEDIA_PROVIDERS: dict[str, _MediaSessionProvider] = {_claude.name: _claude}


def provider_for(name: str) -> AgentProvider:
    try:
        return PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"unknown agent provider {name!r} — known providers: {known}") from None


def available_providers() -> list[AgentProvider]:
    """Only the CLIs actually installed — the set a spawn request can legitimately name."""
    return [p for p in PROVIDERS.values() if p.available()]
