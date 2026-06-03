import functools
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings

from interact.data import PackageData
from interact.models import ModelChain, ModelRole

DEFAULT_LIMIT = 50
LOG_MAXLEN = 1000


class Config(BaseSettings):
    model_config = {"env_prefix": "INTERACT_"}

    image_model: str = ""
    video_model: str = ""
    component_model: str = ""
    # Fallback model chains (comma-separated litellm ids) tried, in order, when the primary
    # model errors. Empty → the bundled per-role recommendations are used as the defaults.
    image_fallbacks: str = ""
    component_fallbacks: str = ""
    video_fallbacks: str = ""
    # Detection refinement (extra VLM passes over dense strips / quadrants to recover missed
    # elements). Improves recall but multiplies VLM calls — set false for faster detection.
    detection_refine: bool = True
    headless: bool = True
    slow_mo: int = 0
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    viewport_width: int = 1280
    viewport_height: int = 720
    screenshot_dump_dir: Path | None = None  # explicit per-run override of the dump base
    # Base dir for interact's local output: the usage log (debug_dir/logs/usage.jsonl) and
    # tool debug artifacts. Default ~/.interact for everyone; override with INTERACT_DEBUG_DIR
    # (e.g. point it at a project's out/ when working locally). screenshot_dump_dir wins if set.
    debug_dir: Path = Path.home() / ".interact"
    video_fps: int = 5
    video_duration: float = 3.0
    max_tokens: int | None = None
    wait_timeout: int = 10000
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
        return self.image_model

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
