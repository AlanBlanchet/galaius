import functools
import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode
from interact_core.tool_settings import VLM_MIN_DIM_DEFAULT, VLM_MAX_DIM_DEFAULT

from interact.agents.providers import MEDIA_PROVIDERS
from interact.criteria import Criteria, CriteriaError
from interact.models import CircuitBreaker, Model, ModelRole

DEFAULT_LIMIT = 50
LOG_MAXLEN = 1000


def _safe_dir_name(name: str) -> str:
    """Filesystem-safe directory name; chars outside [A-Za-z0-9._-] become '_'."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_-. ") or "default"


def _session_custom_title(session_id: str, home: str) -> str | None:
    """User-set title of a Claude Code session, from its transcript under
    ``~/.claude/projects`` (same store ``scan_client_errors.py`` reads). Session id is unique
    so glob finds the file regardless of slug. Only ``custom-title`` lines are parsed (cheap
    pre-filter); LAST one wins (renames)."""
    matches = glob.glob(str(Path(home) / ".claude" / "projects" / "*" / f"{session_id}.jsonl"))
    if not matches:
        return None
    custom = None
    try:
        with open(matches[0], encoding="utf-8") as f:
            for line in f:
                if '"custom-title"' in line:
                    try:
                        custom = json.loads(line).get("customTitle") or custom
                    except ValueError:
                        pass
    except OSError:
        return None
    return custom


@functools.lru_cache(maxsize=16)
def _resolve_session_name(session_id: str, project_dir: str, cwd: str, home: str) -> str:
    """Calling session's log-folder name: custom-title, else project/cwd basename, else
    'default'. Pure function of inputs (lru_cache safe). Cached — title lookup re-reads a
    large transcript."""
    title = _session_custom_title(session_id, home) if session_id else None
    base = Path(project_dir or cwd).name if (project_dir or cwd) else ""
    return _safe_dir_name(title or base or "default")


def caller_session_name() -> str:
    """Claude Code session name driving interact, for per-session logs: custom-title
    (e.g. 'Aino') if resolvable, else CLAUDE_PROJECT_DIR / cwd basename, else 'default'.
    Title wins over basename — it's what the user set and sees."""
    return _resolve_session_name(
        os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        os.environ.get("CLAUDE_PROJECT_DIR", ""),
        os.getcwd(),
        str(Path.home()),
    )

# Quality tiers for review_ui/verify_ui quality=. Agent picks by STAKES not model name: low =
# quick glance, critical = final pre-ship sign-off. Maps to a model (sovereign for low/medium,
# best-available frontier for high/critical) + extra rigor.
QUALITY_TIERS = ("low", "medium", "high", "critical")

# Every role interact resolves a model for gets its OWN requirement, per
# ``interact_core.tool_settings.PortableToolSettingsValues``. An unset field falls back to the
# bare capability that made the role usable at all — never a literal model id (a criterion
# replaces the pin, it does not hide behind one). "Sovereign" names the cheap/self-hostable tier
# (review_ui/verify_ui low/medium) rather than a specific vendor; cheapest-clearing is the whole
# point, so the default criterion carries no price bar of its own — a person who wants one writes
# ``cap.vlm and price.in < 1`` themselves.
#
# ``video`` defaults to the loosest bar, ``cap.vlm``, not ``cap.video``: interact frame-samples a
# recording for any VLM, native video is a nicer path (fewer round trips) but never a hard
# requirement — defaulting to ``cap.video`` would leave an OpenAI-only user with no video model
# at all. A person who wants native video specifically writes ``video.criteria = "cap.video"``.
_ROLE_DEFAULT_CRITERIA: dict[ModelRole, str] = {
    "image": "cap.vlm",
    "component": "cap.gui_grounding",
    "video": "cap.vlm",
    "audio": "cap.audio",
}
_CLAUDE_MEDIA_DEFAULT_CRITERIA = "cap.vlm"
_TIER_SOVEREIGN_DEFAULT_CRITERIA = "cap.vlm"


def _default_media_provider_order() -> tuple[str, ...]:
    """Registry order is the one default; adding a real provider makes it configurable at once."""
    return tuple(MEDIA_PROVIDERS)


class Config(BaseSettings):
    model_config = {"env_prefix": "INTERACT_"}

    # One requirement per role — never a pinned model id, never a separate fallback list: a
    # criterion's own ranking already gives every clearing model in order, so the second-ranked
    # entry IS the fallback (interact_core.tool_settings). Mirrors
    # ``PortableToolSettingsValues`` field-for-field so a server-synced value round-trips.
    image_criteria: str = ""
    video_criteria: str = ""
    component_criteria: str = ""
    audio_criteria: str = ""
    # Backend selects transport; billing decides if interact may call a metered API. Vendor
    # CLIs can consume account credits past plan allowance; a provider missing from the
    # confirmation list still runs, with one warning per process.
    media_backend: Literal["auto", "session", "api"] = "auto"
    media_billing: Literal["session_only", "api_allowed"] = "session_only"
    media_session_no_extra_usage_confirmed_for: Annotated[tuple[str, ...], NoDecode] = ()
    media_provider_order: Annotated[tuple[str, ...], NoDecode] = Field(
        default_factory=_default_media_provider_order
    )
    media_timeout: int = 120
    media_max_items: int = 16
    media_max_total_bytes: int = 50 * 1024 * 1024
    media_max_context_chars: int = 32 * 1024
    prompt_endpoint: str = ""
    prompt_account: str = ""
    prompt_token: str = ""
    prompt_token_file: Path | None = None
    prompt_cache: Path = Path.home() / ".interact" / "prompts.sqlite3"
    claude_media_criteria: str = ""
    # Sovereign-tier requirement (see QUALITY_TIERS); empty → cheapest VLM (self-hosted models
    # sort first on price with no key needed). Override: INTERACT_TIER_SOVEREIGN_CRITERIA.
    tier_sovereign_criteria: str = ""
    #: Shared across every role above — a weight names a benchmark VARIABLE (``aa.intelligence``),
    #: not a role, so a role-specific copy would only ever hold the same text.
    criteria_weights: str = ""
    headless: bool = True
    slow_mo: int = 0
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    viewport_width: Annotated[int, Field(ge=1)] = 1280
    viewport_height: Annotated[int, Field(ge=1)] = 720
    # Set: browser sessions persist profile (cookies, localStorage, login) under
    # <browser_profile_dir>/<session>, instead of the default ephemeral context (logs out every
    # launch). Lets an authenticated flow use the reliable DOM-ref path instead of the flaky
    # desktop-window VLM path (#43). Own subdir per session — Playwright locks a user-data-dir
    # to one running context. Override: INTERACT_BROWSER_PROFILE_DIR.
    browser_profile_dir: Path | None = None
    screenshot_dump_dir: Path | None = None  # explicit per-run override of the dump base
    # Base dir for local output: usage log (debug_dir/usage.jsonl), per-session dumps
    # (debug_dir/sessions/…). Default ~/.interact/out, kept under out/ so root stays clean.
    # Override: INTERACT_DEBUG_DIR. screenshot_dump_dir wins if set.
    debug_dir: Path = Field(default_factory=lambda: Path.home() / ".interact" / "out")
    video_fps: int = 5
    video_duration: float = 3.0
    # Cost cap: recording sampled to at most this many evenly-spaced frames before the VLM, so
    # spend bounds by frame count not clip length — enough to follow UI flow without per-second cost.
    video_max_frames: int = 12
    max_tokens: int | None = None
    wait_timeout: int = 10000
    # Auto-close a browser idle this many seconds (no tool call), freeing Chromium + driver;
    # reopens lazily on next use (non-default session loses cookies/login on close). 0 disables.
    # Override: INTERACT_SESSION_IDLE_TTL.
    session_idle_ttl: int = 900
    # Same for the nested sandbox — its Xephyr is a VISIBLE desktop window (annoys per idle-
    # minute unlike a headless browser), so a shorter default. Abandoned sandbox auto-closes
    # (launch_app respawns); a live recording blocks reaping. 0 disables. Override:
    # INTERACT_SANDBOX_IDLE_TTL.
    sandbox_idle_ttl: int = 300
    # Refresh live model catalog (OpenRouter) + benchmark scores (Artificial Analysis) in the
    # background on server start, so the dashboard shows current data, not stale cache. False:
    # never reach those APIs; panels serve last cache and say how old. Override:
    # INTERACT_REFRESH_LIVE_DATA.
    refresh_live_data: bool = True
    # Automatic upgrades (`interact upgrade`): a signed release from the Interact server this
    # computer signed in to is installed beside the running one, and every long-lived process moves
    # to it at its next quiet moment. Local to this computer: never synced from a server (a server
    # must not be able to turn upgrades back on or hold a computer on an old build).
    auto_upgrade: bool = True
    # Hold this computer on the build with this commit (7+ hex characters of it): installed when
    # the server offers it, otherwise the running build stays. Empty: the newest signed release. A
    # commit, not a version: one version number spans many releases. Checked where it is used
    # (`UpgradeCheck`): a bad value is reported there, never stops a process from starting.
    upgrade_pin: str = ""
    upgrade_check_seconds: Annotated[int, Field(ge=30)] = 300
    # Fall back to GitHub releases when no Interact server is set up (never on a server error).
    upgrade_github: bool = False
    vlm_max_dim: int = VLM_MAX_DIM_DEFAULT
    vlm_min_dim: int = VLM_MIN_DIM_DEFAULT
    detection_max_retries: int = 3  # judge-driven re-detection passes to recover missed elements
    # Every agent interact starts, with everything it runs (MCP servers, browsers, test workers,
    # dev servers), shares ONE systemd user slice capped at these shares of the machine, so agents
    # together can never starve the desktop: the kernel throttles them past `high`, and the OOM
    # killer takes an agent before the editor. Linux with a systemd user manager; elsewhere the
    # agent runs uncapped. Percent of physical RAM / of all CPU cores.
    agent_ceiling: bool = True
    agent_memory_high_percent: Annotated[int, Field(ge=1, le=100)] = 40
    agent_memory_max_percent: Annotated[int, Field(ge=1, le=100)] = 50
    agent_cpu_percent: Annotated[int, Field(ge=1, le=100)] = 75
    # Relative to the desktop's default 100: under contention the editor gets 5x the CPU.
    agent_cpu_weight: Annotated[int, Field(ge=1, le=10000)] = 20
    # "local": drives the real session (uinput, system-wide). "nested": isolated Xephyr display
    # (xdotool) — sandbox that never touches the user's real windows or cursor.
    desktop_target: Literal["local", "nested"] = "local"
    nested_display: int = 99
    nested_size: Annotated[str, Field(pattern=r"^[1-9]\d*x[1-9]\d*$")] = "1280x800"
    # For "nested" target: visible X server (Xephyr, default — watch the agent) or headless
    # (Xvfb, for CI/servers, no window).
    nested_headless: bool = True

    @field_validator(
        "debug_dir", "screenshot_dump_dir", "browser_profile_dir", "prompt_cache",
        "prompt_token_file", mode="after"
    )
    @classmethod
    def _expand_user(cls, value: Path | None) -> Path | None:
        """Expand ``~`` once, at the boundary where the value enters.

        These fields are free text everywhere they're set — config TUI, VS Code settings UI,
        a hand-edited ``config.env`` — and their descriptions advertise ``~/.interact/out``, so
        users type a tilde. Without this, ``INTERACT_DEBUG_DIR=~/.interact`` becomes a literal
        ``Path("~/.interact")`` and every write lands in ``./~/.interact`` relative to wherever
        the server happened to start.
        """
        if value is None:
            return value
        try:
            return value.expanduser()
        except RuntimeError as exc:  # "~nosuchuser/out" — pydantic only wraps ValueError
            raise ValueError(f"cannot expand '~' in {value}: {exc}") from exc

    @field_validator(
        "media_provider_order", "media_session_no_extra_usage_confirmed_for", mode="before"
    )
    @classmethod
    def _parse_media_provider_list(cls, value, info) -> tuple[str, ...]:
        if isinstance(value, str):
            providers = tuple(part.strip() for part in value.split(",") if part.strip())
        elif isinstance(value, (tuple, list)):
            providers = tuple(str(part).strip() for part in value if str(part).strip())
        else:
            raise ValueError("media provider order must be a comma-separated list")
        if not providers and info.field_name == "media_provider_order":
            raise ValueError("media provider order cannot be empty")
        if len(set(providers)) != len(providers):
            raise ValueError(f"{info.field_name} contains a duplicate")
        unknown = [name for name in providers if name not in MEDIA_PROVIDERS]
        if unknown:
            raise ValueError(f"unsupported media provider: {', '.join(unknown)}")
        return providers

    @field_validator(
        "media_timeout", "media_max_items", "media_max_total_bytes", "media_max_context_chars"
    )
    @classmethod
    def _positive_media_limit(cls, value: int, info) -> int:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        return value

    @model_validator(mode="after")
    def _check_dim_bounds(self):
        if self.vlm_min_dim > self.vlm_max_dim:
            raise ValueError(
                f"vlm_min_dim ({self.vlm_min_dim}) must be <= vlm_max_dim ({self.vlm_max_dim})"
            )
        if self.media_backend == "api" and self.media_billing == "session_only":
            raise ValueError("media backend 'api' conflicts with session_only billing")
        return self

    @property
    def usage_log(self) -> Path:
        """The one global VLM-usage log, at ``<debug_dir>/usage.jsonl`` (relocates with debug_dir)."""
        return self.debug_dir / "usage.jsonl"

    def session_log_dir(self) -> Path:
        """Per-caller output root: ``<debug_dir>/sessions/<session>/<date>`` — organised BY
        SESSION, not a flat 'logs' pile. ``<session>`` from ``caller_session_name()``;
        ``<date>`` is today. Every dump interact writes for a run lands here."""
        return self.debug_dir / "sessions" / caller_session_name() / datetime.now().strftime("%Y-%m-%d")

    def media_workspace_root(self) -> Path:
        """User-owned root for temporary artifacts shared by every media transport."""
        return self.session_log_dir()

    def media_model_for(self, provider: str) -> str:
        """The best model THIS session provider can actually run for its criterion, or blank
        when nothing qualifies — never the criterion text itself handed to a vendor CLI's
        ``--model`` flag, which expects a real id."""
        try:
            field = MEDIA_PROVIDERS[provider].media_model_field
        except KeyError:
            raise ValueError(f"unknown media provider: {provider}") from None
        text = (getattr(self, field, "") or _CLAUDE_MEDIA_DEFAULT_CRITERIA).strip()
        try:
            criterion = Criteria.parse(text)
        except CriteriaError:
            return ""
        chosen = criterion.choose(
            available_only=False,
            runnable=lambda model: MEDIA_PROVIDERS[provider].can_run(model, {}),
            weights=self.criteria_weights,
        )
        return MEDIA_PROVIDERS[provider].model_id_for(chosen) if chosen else ""

    def media_sessions_enabled(self) -> bool:
        return self.media_backend != "api"

    def media_api_enabled(self) -> bool:
        return self.media_backend != "session" and self.media_billing == "api_allowed"

    def extra_usage_warning(self, provider: str) -> str | None:
        """Why running ``provider``'s session may bill past the plan, or None once the operator
        confirmed its account-side extra usage off. Advisory only: interact cannot read that
        account state, so it warns instead of refusing."""
        if provider in self.media_session_no_extra_usage_confirmed_for:
            return None
        return (
            f"extra usage not confirmed off — {MEDIA_PROVIDERS[provider].no_extra_usage_guidance}; "
            f"add {provider} to media.noExtraUsageConfirmedFor to silence this warning"
        )

    def criteria_for(self, role: ModelRole) -> str:
        """This role's own requirement, falling back to the bare capability that made it usable
        at all when the user configured nothing — never blank, so a criterion is always
        resolvable and a role never silently means "no requirement"."""
        return (getattr(self, f"{role}_criteria") or _ROLE_DEFAULT_CRITERIA[role]).strip()

    def _role_criterion(self, role: ModelRole) -> Criteria:
        criterion = Criteria.parse(self.criteria_for(role))
        criterion.validate_weights(self.criteria_weights)
        return criterion

    def ranked_models(
        self, role: ModelRole, breaker: CircuitBreaker | None = None
    ) -> list[Model]:
        """Every model this role's criterion clears, best/cheapest first. The FULL ranking, not
        just the top pick — the second entry already IS the fallback a separate stored chain
        used to duplicate (interact_core.tool_settings)."""
        criterion = self._role_criterion(role)
        runnable = (
            (lambda model: model.is_available() and not breaker.tripped(model.id))
            if breaker is not None else None
        )
        return criterion.ranked(available_only=True, runnable=runnable, weights=self.criteria_weights)

    def resolve_model(
        self, role: ModelRole, override: str = "", breaker: CircuitBreaker | None = None
    ) -> str:
        """Single resolution site for a role's model id — nothing downstream ever runs with an
        empty id. Precedence:

        1. explicit per-call ``override`` (the agent's ``model=`` argument),
        2. else the best model that clears this role's criterion right now (available key,
           circuit not tripped).

        A criterion that clears nothing REFUSES loudly, naming the closest miss (``explain``) —
        never a substitute id nobody asked for, and never a silent empty string flowing on into
        the VLM call (the old "[Vision not configured]" bug).
        """
        if override:
            return override
        ranked = self.ranked_models(role, breaker)
        if ranked:
            return ranked[0].id
        criterion = self._role_criterion(role)
        raise RuntimeError(
            f"no model available for role {role!r} against {criterion!s}: "
            f"{criterion.explain(available_only=True)}"
        )

    def explain_model(self, role: ModelRole) -> str:
        """Why this role resolves the way it does — which term excluded everyone, how close
        anyone got. Read by ``interact doctor`` beside the resolved id; a chosen model alone
        can't answer "are we using the best we have"."""
        return self._role_criterion(role).explain(available_only=True)

    def resolve_quality_model(self, quality: str) -> str:
        """Map a quality tier to a model PREFERENCE. low/medium prefer the sovereign tier's
        cheapest clearing model (self-hosted sorts first at zero cost); high/critical fall
        through to normal best-available resolution. Returns "" when the tier is normal or
        nothing clears the sovereign criterion — a graceful preference layered on
        ``resolve_model``, never a hard pin erroring on a missing key. Pass the result as the
        per-call override.
        """
        if quality not in ("low", "medium"):
            return ""
        text = (self.tier_sovereign_criteria or _TIER_SOVEREIGN_DEFAULT_CRITERIA).strip()
        try:
            criterion = Criteria.parse(text)
        except CriteriaError:
            return ""
        chosen = criterion.choose(available_only=True, weights=self.criteria_weights)
        return chosen.id if chosen else ""
