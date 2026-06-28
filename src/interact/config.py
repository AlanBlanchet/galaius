import functools
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings

from interact.data import PackageData
from interact.models import CircuitBreaker, ModelChain, ModelRole

DEFAULT_LIMIT = 50
LOG_MAXLEN = 1000


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
    # Base dir for interact's local output: the usage log (debug_dir/logs/usage.jsonl) and
    # tool debug artifacts. Default ~/.interact for everyone; override with INTERACT_DEBUG_DIR
    # (e.g. point it at a project's out/ when working locally). screenshot_dump_dir wins if set.
    debug_dir: Path = Path.home() / ".interact"
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
    # fresh — cookies/login are not preserved across the close). 0 disables. Stops a long-lived MCP
    # server (one per open editor window) from piling up idle browsers that spin CPU on a left-open
    # page. Override with INTERACT_SESSION_IDLE_TTL.
    session_idle_ttl: int = 900
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
        """Where VLM usage is recorded (under debug_dir, so it relocates with it)."""
        return self.debug_dir / "logs" / "usage.jsonl"

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
