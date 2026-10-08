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
import shutil
import subprocess
import sys
import time
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Annotated, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from galaius.agents.catalog import AgentCatalog
from galaius.agents.events import TOKEN_FIELDS, AgentEvent, TokenUsage, UsageLedger
from galaius.agents.profiles import overlay_for
from galaius.agents.vocabulary import ApprovalIntent, TouchScope, ThinkingLevel, vocabulary_for
from galaius.models import Model
from galaius.processes import run_isolated_process

#: Set (to anything) in the environment of an agent working in a folder whose own agent settings
#: must not load - a workspace cloned from the web: its hooks and permission rules are code and
#: policy its owner never read. Children inherit it, so a subagent there obeys it too.
PROJECT_SETTINGS_OFF = "GALAIUS_PROJECT_SETTINGS_OFF"

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


def _clip(text: str, limit: int = _CLIP) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}… (+{len(text) - limit} chars)"


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
    auth_home_env: ClassVar[tuple[str, ...]] = ()
    permission_option: ClassVar[str] = "--permission-mode"
    #: How this CLI is told to load none of the folder's own settings (`PROJECT_SETTINGS_OFF`);
    #: None: it cannot be, so it never starts or resumes where they must not load.
    folder_settings_off: ClassVar[tuple[str, ...] | None] = None
    #: Whether a turn of this CLI reads a message sent while it works after its current tool call
    #: (`command` / `resume_command` take `inbox_hook`); otherwise the message waits for the turn's end.
    reads_mid_turn: ClassVar[bool] = False

    def _setting_sources(self, environment: Mapping[str, str] | None = None) -> list[str]:
        """`folder_settings_off` when the folder's own settings must not load (`PROJECT_SETTINGS_OFF`
        in `environment`, the one the CLI runs in; this process's own when None), else nothing."""
        if not (os.environ if environment is None else environment).get(PROJECT_SETTINGS_OFF):
            return []
        if self.folder_settings_off is None:
            raise UnsupportedToolPolicy(f"{self.name} cannot run in a workspace cloned from the web: its folder's own settings cannot be switched off")
        return list(self.folder_settings_off)
    #: Provider advertises whether its initial-prompt CLI has a native image attachment flag.
    #: The concrete provider still verifies the installed binary before using it.
    can_attach_images: ClassVar[bool] = False

    def available(self) -> bool:
        """Is the CLI installed? (Being logged in is the CLI's business, never ours.)"""
        return shutil.which(self.binary) is not None

    def executable(self) -> str:
        """Resolve the installed executable for a CLI status probe."""
        found = shutil.which(self.binary)
        if found is None:
            raise FileNotFoundError(f"{self.name} CLI is not installed")
        return str(Path(found).resolve())

    def subscription_env(
        self, base: dict[str, str] | None = None, *, temp_dir: Path | None = None
    ) -> dict[str, str]:
        """Minimal environment for a subscription-authenticated child.

        API keys, auth-token overrides and base URLs are absent — an existing API key can't
        silently take precedence over the user's Claude.ai/ChatGPT subscription, and unrelated
        credentials stay out of child tools and diagnostics.
        """
        source = os.environ if base is None else base
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

    async def _auth_status(self, env: dict[str, str], *, timeout: float) -> tuple[int, str, str] | None:
        """Run the CLI's own auth-status command without reading a credential store."""
        argv = self.auth_command()
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
        return status is not None and status[0] == 0 and self.accepts_subscription_auth(*status[1:])

    async def authenticated(self, env: dict[str, str], *, timeout: float = 10) -> bool | None:
        """CLI login state; None means its status check could not complete."""
        status = await self._auth_status(env, timeout=timeout)
        return None if status is None else status[0] == 0 and self.accepts_any_auth(*status[1:])

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

    def validate_tool_policy(self, allowed_tools: list[str], denied_tools: tuple[str, ...], *,
                             coarse_accepted: bool = False) -> None:
        """Refuse a role restriction this CLI cannot express, UNLESS the caller already recorded
        an explicit acceptance of coarser (sandbox-scope, not per-tool) enforcement instead — see
        :meth:`galaius.agents.policy.Policy.accepts_coarse_tool_policy`. A provider that genuinely
        cannot express ANY part of the restriction, coarse or not, ignores ``coarse_accepted``;
        only a provider whose own docstring says otherwise (:class:`CodexProvider`) honours it."""
        validate_denied_tools(denied_tools)
        if allowed_tools or denied_tools:
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

    def image_attachment_support(self) -> bool:
        """Whether this installed provider can receive image paths on its initial prompt."""
        return self.can_attach_images

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

    def queue_command(self, session_id: str, message: str) -> list[str]:
        """The argv that queues ``message`` for a session already served by this CLI."""
        raise NotImplementedError(f"{type(self).__name__} cannot queue a message")

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
_HARNESS_BOOKKEEPING = frozenset({"thinking_tokens", "hook_started", "hook_response"})

class ClaudeCodeProvider(AgentProvider):
    """Claude Code, driven through its documented headless mode.

    Flags verified against `claude --version` 2.1.233. `--output-format stream-json` REQUIRES
    `--verbose`; without it the CLI refuses and the run emits nothing parseable.
    """

    name = "claude"
    folder_settings_off = ("--setting-sources", "user")
    reads_mid_turn = True
    native_providers = frozenset({"anthropic"})
    media_model_field = "claude_media_criteria"
    auth_home_env = ("CLAUDE_CONFIG_DIR",)
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

    def model_id_for(self, model: Model) -> str:
        """Derive Claude Code's hyphenated version spelling for every future catalog row."""
        model_id = super().model_id_for(model)
        return re.sub(r"(?<=\d)\.(?=\d)", "-", model_id)

    def auth_command(self) -> list[str]:
        return [self.executable(), "auth", "status"]

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
            for line in stdout_bytes.decode(errors="replace").splitlines()
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

    def validate_tool_policy(self, allowed_tools: list[str], denied_tools: tuple[str, ...], *,
                             coarse_accepted: bool = False) -> None:
        # Claude enforces the FULL list itself (`--agents`/`--allowedTools`/`--disallowedTools`
        # below); there is nothing coarser to fall back to, so acceptance of one is moot here.
        validate_denied_tools(denied_tools)

    def role_arguments(self, agent: str | None, agent_prompt: str | None,
                       allowed_tools: list[str] | None, denied_tools: tuple[str, ...]) -> list[str]:
        self.validate_tool_policy(allowed_tools or [], denied_tools)
        arguments = []
        if agent is not None:
            self.validate_agent_name(agent)
        if allowed_tools or agent_prompt is not None:
            if not agent:
                raise UnsupportedToolPolicy("A named role is required to restrict Claude tools")
            if agent_prompt is None:
                agent_prompt = self.definition_prompt(agent)
            definition = {"description": f"Galaius role {agent}", "prompt": agent_prompt}
            if allowed_tools:
                definition["tools"] = allowed_tools
            arguments += ["--agents", json.dumps({agent: definition})]
        if agent:
            arguments += ["--agent", agent]
        if denied_tools:
            arguments += ["--disallowedTools", ",".join(denied_tools)]
        return arguments

    def command(self, task: str, *, cwd: str, model: str | None, mcp_config: str | None,
                run_id: str, agent: str | None = None,
                permission_mode: str | None = None,
                allowed_tools: list[str] | None = None,
                reasoning: str | None = None,
                agent_prompt: str | None = None, denied_tools: tuple[str, ...] = (),
                image_paths: tuple[Path, ...] = (), coarse_accepted: bool = False,
                base_url: str | None = None, environment: Mapping[str, str] | None = None,
                inbox_hook: str | None = None) -> list[str]:
        # `base_url` is unused here: Claude Code only ever speaks the Anthropic wire protocol
        # (ANTHROPIC_BASE_URL, read from its own process env, set by resolve_model's overlay) —
        # never OpenAI's, so a routed hf/vllm endpoint is never handed to this binary at all
        # (see CodexProvider.can_run / ClaudeCodeProvider.can_run's ANTHROPIC_BASE_URL check).
        if image_paths:
            self._image_args(image_paths)
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
        argv += self.role_arguments(agent, agent_prompt, allowed_tools, denied_tools)
        if mcp_config:
            argv += ["--mcp-config", mcp_config]
        argv += self._permission_flag(permission_mode) + self._setting_sources(environment) + self.inbox_arguments(inbox_hook)
        if reasoning is not None:
            argv += ["--effort", self.provider_thinking_level(reasoning)]
        # The prompt goes LAST, after "--": one starting with "-" (a markdown bullet) is the
        # prompt, never an option Claude refuses ("unknown option").
        return [*argv, "--", task]

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
        inbox_hook: str | None = None,
    ) -> list[str]:
        """Continue an existing session using its provider session id (`fork_to`: as a copy)."""
        # base_url unused — see command()'s docstring note.
        return [
            self.binary, "-p",
            "--resume", session_id,
            *(["--fork-session", "--session-id", fork_to] if fork_to else []),
            "--output-format", "stream-json",
            "--verbose",
        ] + (["--model", model] if model else []) + self._permission_flag(permission_mode) + self._setting_sources() + self.inbox_arguments(inbox_hook) + self.role_arguments(agent, agent_prompt, allowed_tools, denied_tools) + (["--mcp-config", mcp_config] if mcp_config else []) + ["--", message]

    @staticmethod
    def inbox_arguments(hook: str | None) -> list[str]:
        """`hook` run after each of the turn's own tool calls (`PostToolUse`; what it prints as
        `additionalContext` the model reads before its next step). Settings given on the command
        line load whatever `--setting-sources` says, so a cloned workspace keeps it too."""
        if hook is None:
            return []
        return ["--settings", json.dumps({"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook}]}]}})]

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
        ledger = UsageLedger() if ledger is None else ledger
        kind = raw.get("type", "")
        sid = raw.get("session_id")

        if kind == "system" and raw.get("subtype") in _HARNESS_BOOKKEEPING:
            return None  # about the harness's own machinery, not about what the agent did

        if kind == "system" and raw.get("subtype") == "task_started":
            return AgentEvent(kind="spawn", session_id=sid, raw_type=kind,
                              text=str(raw.get("description") or raw.get("agent_type") or "subagent"))

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
            if tool:
                return AgentEvent(kind="tool", tool=tool, tool_input=tool_input, tool_id=tool_id,
                                  session_id=sid, raw_type=kind, **live)
            if thinking and not any(t.strip() for t in texts):
                return AgentEvent(kind="thinking", text=_clip("".join(thinking)),
                                  session_id=sid, raw_type=kind, **live)
            return AgentEvent(kind="text", text="".join(texts), session_id=sid, raw_type=kind,
                              **live)

        if kind == "user":
            # Tool results come back as a USER turn — that is the other half of a transcript.
            msg = raw.get("message") or {}
            for block in msg.get("content") or []:
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
                # conversation. Unnamed, an activity view shows only the agent talking.
                if block.get("type") == "text":
                    return AgentEvent(kind="prompt", text=_clip(str(block.get("text") or "")),
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
            return AgentEvent(kind="rate_limit", session_id=sid, raw_type=kind, text=line)

        if kind == "result":
            settled, cost = self._settle_result(raw, ledger)
            failed = bool(raw.get("is_error"))
            structured = raw.get("structured_output")
            has_final = structured is not None or bool(raw.get("result"))
            detail = json.dumps(structured) if structured is not None else str(
                raw.get("result") or raw.get("stop_reason") or raw.get("subtype") or ""
            )
            return AgentEvent(
                kind="error" if failed else "done",
                session_id=sid, raw_type=kind,
                text=detail,
                cost_usd=cost,
                final_text=has_final,
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
    env: dict[Literal["GALAIUS_PARENT_RUN_ID"], Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class _CodexMesh(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    servers: dict[Literal["galaius"], _CodexMeshServer] = Field(alias="mcpServers", min_length=1)


class _CodexMcpRegistration(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str = Field(min_length=1)


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
    native_providers = frozenset({"openai", "chatgpt"})
    binary = "codex"
    can_resume = True
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
            "-c", f"{prefix}.env_key={json.dumps('GALAIUS_OPENAI_COMPAT_KEY')}",
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

    def auth_command(self) -> list[str]:
        return [self.executable(), "login", "status"]

    def accepts_subscription_auth(self, stdout: str, stderr: str) -> bool:
        lines = [line.strip().lower() for line in f"{stdout}\n{stderr}".splitlines()]
        return any("logged in using chatgpt" in line and "api key" not in line for line in lines)

    def accepts_any_auth(self, stdout: str, stderr: str) -> bool:
        return any(line.strip().lower().startswith("logged in using ")
                   for line in f"{stdout}\n{stderr}".splitlines())

    def has_mcp_server(self, name: str, *, cwd: str) -> bool:
        """Ask Codex to resolve all configuration layers; retain registration names only."""
        try:
            result = subprocess.run(
                [self.executable(), "mcp", "list", "--json"],
                cwd=cwd, capture_output=True, text=True, timeout=10,
            )
            if result.returncode:
                raise ValueError("Codex configuration lookup failed")
            registrations = TypeAdapter(list[_CodexMcpRegistration]).validate_json(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            # Provider output and validation errors can contain environment/configuration values.
            raise ValueError("Cannot inspect Codex MCP configuration safely") from None
        return any(registration.name == name for registration in registrations)

    @staticmethod
    def mesh_arguments(mcp_config: str | None) -> list[str]:
        """Project the launcher's credential-free stdio mesh without granting access."""
        if mcp_config is None:
            return []
        try:
            server = _CodexMesh.model_validate_json(mcp_config).servers["galaius"]
        except ValueError:
            # Validation errors include input values; never echo rejected environment data.
            raise ValueError("Invalid credential-free Codex mesh configuration") from None
        return [
            "-c", f"mcp_servers.galaius.command={json.dumps(server.command, ensure_ascii=False)}",
            "-c", f"mcp_servers.galaius.args={json.dumps(server.args, ensure_ascii=False)}",
            "-c", "mcp_servers.galaius.env.GALAIUS_PARENT_RUN_ID="
            + json.dumps(server.env["GALAIUS_PARENT_RUN_ID"], ensure_ascii=False),
        ]

    def validate_tool_policy(self, allowed_tools: list[str], denied_tools: tuple[str, ...], *,
                             coarse_accepted: bool = False) -> None:
        validate_denied_tools(denied_tools)
        allowed_native, denied_native = _native_tool_sets(allowed_tools, denied_tools)
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
        self.validate_tool_policy(allowed_tools or [], denied_tools, coarse_accepted=coarse_accepted)
        task = self._inject_definition(agent, task, agent_prompt)
        argv = [self.binary, "exec", "--json", *self.native_delegation_flags, *self.platform_flags(), *self._setting_sources(environment)]
        argv += self.mesh_arguments(mcp_config)
        argv += self._mcp_tool_scope_arguments(allowed_tools or [], denied_tools)
        argv += self._native_sandbox_arguments(allowed_tools or [], denied_tools,
                                               permission_mode=permission_mode, coarse_accepted=coarse_accepted)
        argv += self._openai_compat_arguments(base_url)
        if model:
            argv += ["--model", model]
        if reasoning is not None:
            argv += ["-c", f'model_reasoning_effort="{self.provider_thinking_level(reasoning)}"']
        # The task goes LAST, after "--": one starting with "-" stays the task ("-" alone would
        # otherwise mean "read it from stdin").
        return [*argv, *self._image_args(image_paths), "--", task]

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

    def queue_command(self, session_id: str, message: str) -> list[str]:
        return [self.binary, "queue", "--thread", session_id, "--message", message]

    def resume_command(
        self, session_id: str, message: str, *, model: str | None = None,
        permission_mode: str | None = None, reasoning: str | None = None,
        agent: str | None = None,
        mcp_config: str | None = None, allowed_tools: list[str] | None = None,
        agent_prompt: str | None = None, denied_tools: tuple[str, ...] = (),
        coarse_accepted: bool = False, base_url: str | None = None, fork_to: str | None = None,
    ) -> list[str]:
        """Resume a stopped session with fresh model and reasoning policy.

        ``exec resume`` has no ``--sandbox`` option. The saved Codex session owns that scope;
        callers validate the recorded value before reaching this method and do not widen it here.

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
        self.validate_tool_policy(allowed_tools or [], denied_tools, coarse_accepted=coarse_accepted)
        if agent:
            self.validate_agent_name(agent)
        argv = [self.binary, "exec", "resume", "--json", *self.native_delegation_flags, *self.platform_flags(), *self._setting_sources()]
        argv += self.mesh_arguments(mcp_config)
        argv += self._mcp_tool_scope_arguments(allowed_tools or [], denied_tools)
        argv += self._openai_compat_arguments(base_url)
        # `exec resume --help` has no `--sandbox`; the saved codex session owns that scope from
        # its FIRST turn (`command`, above, now always sets one — never left to codex's own
        # unset-defaults-to-read-only). An explicit `permission_mode` still widens/narrows it as
        # the operator's own per-turn choice; the shell/network flags are config, not session
        # state, so they are resent every turn from the SAME policy, never widened here.
        mapping = _widen_for_coarse_acceptance(
            _classify_native_tools(*_native_tool_sets(allowed_tools or [], denied_tools)), coarse_accepted,
        )
        if permission_mode is not None:
            argv += ["-c", f'sandbox_mode="{self.provider_permission_mode(permission_mode)}"']
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
        return [*argv, "--", session_id, message]

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
                              raw_type=kind, text=_clip(text or line))
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
