import functools
import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings

from interact.data import PackageData
from interact.models import CircuitBreaker, ModelChain, ModelRole

DEFAULT_LIMIT = 50
LOG_MAXLEN = 1000


def _safe_dir_name(name: str) -> str:
    """A filesystem-safe directory name (collapse anything outside [A-Za-z0-9._-] to '_')."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_-. ") or "default"


def _session_custom_title(session_id: str, home: str) -> str | None:
    """The user-set title of a Claude Code session, read from its transcript under
    ``~/.claude/projects`` — the same store ``scan_client_errors.py`` reads. The session id is
    unique, so a glob finds the file regardless of how Claude slugs the project dir. Only the small
    ``custom-title`` lines are parsed (cheap string pre-filter), and the LAST one wins (renames)."""
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
    """Resolve the calling session's log-folder name (pure function of its inputs, so lru_cache is
    safe + keyed by env): the Claude session's custom-title, else the project/cwd dir basename, else
    'default'. Cached because the title means re-reading a (large) transcript."""
    title = _session_custom_title(session_id, home) if session_id else None
    base = Path(project_dir or cwd).name if (project_dir or cwd) else ""
    return _safe_dir_name(title or base or "default")


def caller_session_name() -> str:
    """The name of the Claude Code session driving interact, for separating logs per session: its
    user-set custom-title (e.g. 'Aino') when resolvable, else the CLAUDE_PROJECT_DIR / cwd basename,
    else 'default'. The dir basename and the session title can differ — the title is the authoritative
    one the user sees (and set), so it wins."""
    return _resolve_session_name(
        os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        os.environ.get("CLAUDE_PROJECT_DIR", ""),
        os.getcwd(),
        str(Path.home()),
    )

# The "sovereign" GLM-4.5V models the low/medium quality tiers prefer (MIT, open-weight,
# self-hostable) — a strong open VLM, cheap/private, the right default when peak frontier accuracy
# isn't needed. Tried in order; the FIRST whose API key is present wins, so EITHER a z.ai key
# (ZAI_API_KEY → first-party `zai/`) or a Novita key (NOVITA_API_KEY → reseller `novita/`) lights up
# GLM with zero config. An explicit INTERACT_TIER_SOVEREIGN_MODEL overrides the whole list (e.g. a
# self-hosted endpoint id). z.ai is preferred — it's GLM's first-party API.
_SOVEREIGN_MODELS = ("zai/glm-4.5v", "novita/zai-org/glm-4.5v")
_DEFAULT_SOVEREIGN_MODEL = _SOVEREIGN_MODELS[0]  # preferred default (also a back-compat alias)

# Quality tiers for the UI tools (review_ui/verify_ui quality=...): the agent picks by STAKES, not
# by model name — low = quick glance, critical = final pre-ship sign-off. interact maps the tier to
# a model (sovereign for low/medium, best-available frontier for high/critical) + extra rigor.
QUALITY_TIERS = ("low", "medium", "high", "critical")


class Config(BaseSettings):
    model_config = {"env_prefix": "INTERACT_"}

    image_model: str = ""
    video_model: str = ""
    component_model: str = ""
    audio_model: str = ""
    # Fallback model chains (comma-separated litellm ids) tried, in order, when the primary
    # model errors. Empty → the bundled per-role recommendations are used as the defaults.
    image_fallbacks: str = ""
    component_fallbacks: str = ""
    video_fallbacks: str = ""
    audio_fallbacks: str = ""
    # The model the low/medium quality tiers prefer (see QUALITY_TIERS). Empty → GLM-4.5V default
    # (_DEFAULT_SOVEREIGN_MODEL). Override with INTERACT_TIER_SOVEREIGN_MODEL (e.g. a local id).
    tier_sovereign_model: str = ""
    headless: bool = True
    slow_mo: int = 0
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    viewport_width: int = 1280
    viewport_height: int = 720
    # When set, browser sessions persist their profile (cookies, localStorage, login) on disk under
    # <browser_profile_dir>/<session> instead of the default ephemeral context that starts logged
    # out every launch. Lets an authenticated flow run through the reliable DOM-ref path (log in
    # once, stay logged in across restarts) rather than the flaky desktop-window VLM path (#43).
    # Each session gets its own subdir — Playwright locks a user-data-dir to one running context.
    # Override with INTERACT_BROWSER_PROFILE_DIR.
    browser_profile_dir: Path | None = None
    screenshot_dump_dir: Path | None = None  # explicit per-run override of the dump base
    # Base dir for interact's local output: the usage log (debug_dir/usage.jsonl) and per-session debug
    # dumps (debug_dir/sessions/…). Default ~/.interact/out — kept in an `out/` folder so the root stays clean
    # (just config.env + out/) instead of being scattered with timestamped dump dirs. Override with
    # INTERACT_DEBUG_DIR (e.g. a project's out/ when working locally). screenshot_dump_dir wins if set.
    debug_dir: Path = Path.home() / ".interact" / "out"
    video_fps: int = 5
    video_duration: float = 3.0
    # Cost cap for video understanding: a recording is sampled down to at most this many frames
    # (evenly spaced) before going to the VLM, so spend is bounded by frame count, not clip
    # length — enough frames to follow what happened (UI flow, gameplay), without paying per second.
    video_max_frames: int = 12
    max_tokens: int | None = None
    wait_timeout: int = 10000
    # Auto-close a browser session whose browser has sat idle (no tool call) this many seconds,
    # freeing its Chromium + driver; it re-opens lazily on next use (a non-default session starts
    # fresh — cookies/login are not preserved across the close). 0 disables. Override with
    # INTERACT_SESSION_IDLE_TTL.
    session_idle_ttl: int = 900
    # Same idea for the nested sandbox — but its Xephyr is a VISIBLE window on the user's desktop,
    # which annoys per idle-minute in a way an invisible headless browser doesn't, so it defaults
    # shorter. An abandoned sandbox auto-closes (the next launch_app respawns one); a live recording
    # blocks reaping. 0 disables. Override with INTERACT_SANDBOX_IDLE_TTL.
    sandbox_idle_ttl: int = 300
    vlm_max_dim: int = 1280
    vlm_min_dim: int = 768
    detection_max_retries: int = 3  # judge-driven re-detection passes to recover missed elements
    # Desktop target: "local" drives the real session (uinput, system-wide);
    # "nested" runs an isolated Xephyr display (xdotool into it) — for tests / a VM-like
    # sandbox that never touches the user's real windows or cursor.
    desktop_target: Literal["local", "nested"] = "local"
    nested_display: int = 99
    nested_size: str = "1280x800"
    # When the target is "nested": run the X server visible (Xephyr, default — watch the
    # agent) or headless in the background (Xvfb — for CI / servers, no window).
    nested_headless: bool = False

    @model_validator(mode="after")
    def _check_dim_bounds(self):
        if self.vlm_min_dim > self.vlm_max_dim:
            raise ValueError(
                f"vlm_min_dim ({self.vlm_min_dim}) must be <= vlm_max_dim ({self.vlm_max_dim})"
            )
        return self

    @property
    def usage_log(self) -> Path:
        """The one global VLM-usage log, at ``<debug_dir>/usage.jsonl`` (relocates with debug_dir)."""
        return self.debug_dir / "usage.jsonl"

    def session_log_dir(self) -> Path:
        """Per-caller output root: ``<debug_dir>/sessions/<session>/<date>``. The dir is organised BY
        SESSION (not a flat 'logs' pile): ``<session>`` is the calling client's name — a Claude Code
        session's custom-title (e.g. 'Aino'), else the VS Code / project / cwd basename, else 'default'
        — and ``<date>`` is today. Every dump interact writes for a run lands here, dated."""
        return self.debug_dir / "sessions" / caller_session_name() / datetime.now().strftime("%Y-%m-%d")

    def model_for(self, role: ModelRole) -> str:
        if role == "video":
            return self.video_model
        if role == "component":
            return self.component_model
        if role == "audio":
            return self.audio_model
        return self.image_model

    def resolve_model(
        self, role: ModelRole, override: str = "", breaker: CircuitBreaker | None = None
    ) -> str:
        """Single resolution site for a role's model id — the boundary where "which model"
        is decided once, so nothing downstream ever runs with an empty id (the bug behind the
        "[Vision not configured]" returns: the auto path left ``model_for`` empty and that ``""``
        flowed all the way into the VLM call). Precedence:

        1. an explicit per-call ``override`` (the agent's ``model=`` argument),
        2. else the configured pin (``model_for`` — honoured even if its key is missing, so a
           bad pin surfaces a clear auth error rather than being silently swapped out),
        3. else the first *available* model in the role's preference chain (key present, cheapest
           ranked, skipping circuit-broken ones),
        4. else the chain's top preference (so a missing key becomes a clear downstream auth
           error, never an empty-id silent no-op).

        Raises if the catalog is empty (no models.json and no litellm) — fail loud here, at the
        one resolution site, not by leaking a sentinel for deeper code to re-validate.
        """
        if override:
            return override
        configured = self.model_for(role)
        if configured:
            return configured
        chain = self.chain_for(role)
        active = chain.active(breaker)
        if active is not None:
            return active.id
        if chain.preferences:
            return chain.preferences[0].id
        raise RuntimeError(f"no model available for role {role!r}: empty model catalog")

    def resolve_quality_model(self, quality: str) -> str:
        """Map a quality tier to a model PREFERENCE (the "choose the model for me" literal). low/medium
        prefer a sovereign self-host GLM (private, cheap); high/critical fall through to the normal
        best-available resolution. Returns "" when the tier implies normal resolution OR no sovereign
        candidate is reachable — a graceful preference layered on resolve_model, never a hard pin that
        errors on a missing key. Pass the result as the per-call override.

        An explicit tier_sovereign_model is the only candidate (honour the user's pin); otherwise each
        _SOVEREIGN_MODELS id is tried in order and the FIRST whose key is present wins — so a z.ai key
        (`zai/`) and a Novita key (`novita/`) both light up GLM with no configuration."""
        if quality not in ("low", "medium"):
            return ""
        from interact.models import Model

        candidates = (self.tier_sovereign_model,) if self.tier_sovereign_model else _SOVEREIGN_MODELS
        for candidate in candidates:
            if candidate and Model.from_litellm_id(candidate).is_available():
                return candidate
        return ""

    @functools.cached_property
    def _recommendations(self) -> dict[str, list[str]]:
        return PackageData.models_data().get("recommendations", {})

    def fallbacks_for(self, role: ModelRole) -> list[str]:
        """User-configured fallback model ids for a role (empty → use bundled defaults)."""
        raw = getattr(self, f"{role}_fallbacks", "") or ""
        return [model.strip() for model in raw.split(",") if model.strip()]

    def chain_for(self, role: ModelRole) -> ModelChain:
        """Build a model fallback chain for a role (image, component, video): the configured
        primary, then user-configured fallbacks if any, else the bundled recommendations."""
        configured = self.model_for(role)
        recommendations = self.fallbacks_for(role) or self._recommendations.get(role, [])
        return ModelChain.from_config(role, configured, recommendations)
