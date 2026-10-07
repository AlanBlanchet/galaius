"""The one declarative description of galaius's user-configurable settings.

Every front end that lets a user configure galaius — the bare-``galaius`` Textual TUI and the
VS Code extension panel — renders from THIS list instead of each re-declaring fields, labels,
defaults and env-var mappings (which had already drifted: the TUI wrote ``GALAIUS_BROWSER_HEADLESS``
that :class:`~galaius.config.Config` never reads, and TUI/extension disagreed on ``debug.dir``).

A :class:`Setting` is keyed to a real ``Config`` attribute (``field``), so its env-var name and
default derive from the runtime config and can't drift; a test asserts every setting maps to an
existing field. Schema exports to JSON (``PackageData.settings_raw``) for the extension, which
generates its env-map and renders its Configuration panel from the same source.

Front ends consume the common spec and override only presentation when they must (a richer widget,
hiding a field) — the *behaviour* (which key, which env var, the default) stays shared.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, computed_field

from galaius.config.settings import Config

SettingKind = Literal["enum", "bool", "int", "str", "path"]
SettingGroup = Literal["Models", "Desktop", "Browser", "Advanced"]


class Option(BaseModel):
    """One choice for an ``enum`` setting (or a model dropdown): a human label + the stored value."""

    label: str
    value: str


class Setting(BaseModel):
    """A single user-configurable setting, keyed to a ``Config`` attribute so its env-var name and
    default come from the runtime config (no second copy to keep in sync)."""

    key: str  # friendly dotted key used by UserConfig / the extension, e.g. "image.criteria"
    field: str  # the Config attribute this maps to, e.g. "image_criteria" — source of env + default
    label: str
    description: str
    group: SettingGroup
    kind: SettingKind
    role: str | None = None  # the model role this criterion resolves for (image/component/video/audio)
    options: list[Option] | None = None  # for kind="enum"
    @computed_field
    @property
    def minimum(self) -> int | None:
        """Numeric lower bound projected from the runtime Config field, when declared."""
        value = Config.model_json_schema()["properties"][self.field].get("minimum")
        return int(value) if isinstance(value, int | float) else None

    @computed_field
    @property
    def pattern(self) -> str | None:
        """String constraint projected from the runtime Config field, when it declares one."""
        value = Config.model_json_schema()["properties"][self.field].get("pattern")
        return value if isinstance(value, str) else None

    @computed_field
    @property
    def env(self) -> str:
        """The environment variable Config reads for this setting (its single source of truth)."""
        return f"GALAIUS_{self.field.upper()}"

    @computed_field
    @property
    def default(self) -> str:
        """The field's *declared* default as a string — from the Config field definition, NOT a
        live ``Config()`` instance (which would fold in the current GALAIUS_* env and make the
        exported JSON depend on the environment it was generated in). Empty string = "auto /
        unset"; home is collapsed to ``~`` so the export is portable, not the build machine's path."""
        from pathlib import Path

        field = Config.model_fields[self.field]
        value = field.default_factory() if field.default_factory is not None else field.default
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        if isinstance(value, Path):
            # Render path defaults POSIX-style (forward slashes) so exported JSON is byte-identical
            # on every OS — otherwise Windows bakes `~\.galaius` and drifts from the bundled
            # (Linux-generated) settings.json, failing the lockstep check.
            text, home = value.as_posix(), Path.home().as_posix()
        elif isinstance(value, tuple):
            text, home = ",".join(str(part) for part in value), str(Path.home())
        else:
            text, home = str(value), str(Path.home())
        return "~" + text[len(home):] if text.startswith(home) else text


# Ordered by group; the order here is the order shown in every front end.
SETTINGS: list[Setting] = [
    # ── Models ───────────────────────────────────────────────────────────────
    # Every role below carries a CRITERION, never a pinned model id: a sentence the resolver
    # re-reads every call against whatever provider key or CLI session is actually reachable
    # (galaius_core.tool_settings.PortableToolSettingsValues). A blank field falls back to the
    # bare capability that made the role usable at all (Config._ROLE_DEFAULT_CRITERIA).
    Setting(
        key="criteria.weights", field="criteria_weights", group="Models", kind="str",
        label="Criteria weights",
        description="Optional weights over normalized unit-safe criteria only, shared by every "
        "role below; raw accuracy, index, latency, and price units cannot be combined.",
    ),
    Setting(
        key="media.backend", field="media_backend", group="Models", kind="enum",
        label="Media backend",
        description="Where image and sampled-video analysis runs in an isolated sandbox. Auto follows the billing policy.",
        options=[
            Option(label="Auto", value="auto"),
            Option(label="Subscription session", value="session"),
            Option(label="API", value="api"),
        ],
    ),
    Setting(
        key="media.billing", field="media_billing", group="Models", kind="enum",
        label="Media billing policy",
        description="Session only prevents galaius's metered API fallback; vendor CLI account "
        "credits are separate — see the confirmation below.",
        options=[
            Option(label="Session only", value="session_only"),
            Option(label="API allowed", value="api_allowed"),
        ],
    ),
    Setting(
        key="media.noExtraUsageConfirmedFor", field="media_session_no_extra_usage_confirmed_for",
        group="Models", kind="enum", label="No-extra-usage confirmed providers",
        description="Claude confirmation: Usage credits disabled, zero prepaid balance, "
        "and auto-reload off. Unconfirmed providers still run, with one warning per process. "
        "galaius cannot verify this account state.",
        options=[
            Option(label="None (sessions warn)", value=""),
            Option(label="Claude", value="claude"),
        ],
    ),
    Setting(
        key="media.providerOrder", field="media_provider_order", group="Models", kind="enum",
        label="Subscription provider order",
        description="Media-capable subscription CLI, currently Claude.",
        options=[Option(label="Claude", value="claude")],
    ),
    Setting(
        key="media.claudeCriteria", field="claude_media_criteria", group="Models", kind="str",
        label="Claude session model criteria",
        description="Requirement the Claude CLI's own model must clear (e.g. 'cap.vlm'); blank "
        "uses the subscription CLI default.",
    ),
    Setting(
        key="tierSovereign.criteria", field="tier_sovereign_criteria", group="Models", kind="str",
        label="Sovereign-tier criteria",
        description="Requirement for review_ui/verify_ui's low/medium quality tier (cheapest "
        "clearing model wins — self-hosted sorts first at zero cost); blank means any VLM.",
    ),
    Setting(
        key="image.criteria", field="image_criteria", group="Models", kind="str", role="image",
        label="Vision model criteria",
        description="Requirement for screenshots and images (e.g. 'cap.vlm and aa.intelligence "
        "> 80%'); blank means any VLM. Subscription sessions use media.claudeCriteria instead.",
    ),
    Setting(
        key="component.criteria", field="component_criteria", group="Models", kind="str", role="component",
        label="Component model criteria",
        description="Requirement for GUI grounding (falls back to cap.gui_grounding). "
        "Subscription sessions use their media session model.",
    ),
    Setting(
        key="video.criteria", field="video_criteria", group="Models", kind="str", role="video",
        label="Video model criteria",
        description="Requirement for video (falls back to cap.video). Subscription sessions "
        "always receive ordered, timestamped sampled frames; an API model may receive native "
        "video when supported.",
    ),
    Setting(
        key="audio.criteria", field="audio_criteria", group="Models", kind="str", role="audio",
        label="Audio model criteria",
        description="Requirement for transcribe (falls back to cap.audio). Claude subscription "
        "sessions do not hear audio; media.billing must allow this separate path.",
    ),
    # ── Desktop ──────────────────────────────────────────────────────────────
    Setting(
        key="desktop.target", field="desktop_target", group="Desktop", kind="enum",
        label="Desktop target",
        description="Where desktop automation acts: your real session, or an isolated sandbox.",
        options=[
            Option(label="local — your real session", value="local"),
            Option(label="nested — isolated sandbox display", value="nested"),
        ],
    ),
    Setting(
        key="desktop.nestedHeadless", field="nested_headless", group="Desktop", kind="bool",
        label="Nested headless",
        description="Sandbox only: ON = Xvfb in the background (CI/servers); OFF = Xephyr you can watch.",
    ),
    Setting(
        key="desktop.nestedDisplay", field="nested_display", group="Desktop", kind="int",
        label="Nested display",
        description="X display number for the sandbox (e.g. 99 → :99).",
    ),
    Setting(
        key="desktop.nestedSize", field="nested_size", group="Desktop", kind="str",
        label="Nested size",
        description="Sandbox screen size, WIDTHxHEIGHT.",
    ),
    # ── Browser ──────────────────────────────────────────────────────────────
    Setting(
        key="browser.headless", field="headless", group="Browser", kind="bool",
        label="Browser headless",
        description="Run the automation browser without a visible window.",
    ),
    Setting(
        key="browser.type", field="browser_type", group="Browser", kind="enum",
        label="Browser engine",
        description="Which Playwright engine drives browser automation.",
        options=[
            Option(label="Chromium", value="chromium"),
            Option(label="Firefox", value="firefox"),
            Option(label="WebKit", value="webkit"),
        ],
    ),
    Setting(
        key="browser.viewportWidth", field="viewport_width", group="Browser", kind="int",
        label="Viewport width", description="Browser viewport width in pixels.",
    ),
    Setting(
        key="browser.viewportHeight", field="viewport_height", group="Browser", kind="int",
        label="Viewport height", description="Browser viewport height in pixels.",
    ),
    Setting(
        key="browser.slowMo", field="slow_mo", group="Browser", kind="int",
        label="Slow-mo (ms)",
        description="Delay added between browser actions, in ms (0 = full speed; useful when watching).",
    ),
    Setting(
        key="browser.profileDir", field="browser_profile_dir", group="Browser", kind="path",
        label="Persistent profile dir",
        description="When set, browser sessions keep their cookies/login on disk here (under "
        "<dir>/<session>) so you log in once and stay authenticated across restarts — run "
        "authenticated flows through the reliable DOM-ref path instead of driving a logged-in "
        "desktop browser window. Blank = ephemeral (logged out every launch).",
    ),
    # ── Advanced ─────────────────────────────────────────────────────────────
    Setting(
        key="vlm.maxTokens", field="max_tokens", group="Advanced", kind="int",
        label="VLM max tokens",
        description="Cap on VLM output tokens per call (blank = the model's default).",
    ),
    Setting(
        key="media.timeout", field="media_timeout", group="Advanced", kind="int",
        label="Subscription media timeout (s)",
        description="Hard limit for a Claude media-analysis child process.",
    ),
    Setting(
        key="media.maxItems", field="media_max_items", group="Advanced", kind="int",
        label="Media items per request",
        description="Maximum number of image, video, or audio items accepted in one analysis.",
    ),
    Setting(
        key="media.maxTotalBytes", field="media_max_total_bytes", group="Advanced", kind="int",
        label="Media bytes per request",
        description="Maximum aggregate decoded media bytes accepted before any provider runs.",
    ),
    Setting(
        key="media.maxContextChars", field="media_max_context_chars", group="Advanced", kind="int",
        label="Media context characters",
        description="Maximum untrusted page, title, URL, or transcript context characters per request.",
    ),
    Setting(
        key="vlm.waitTimeout", field="wait_timeout", group="Advanced", kind="int",
        label="Action wait timeout (ms)",
        description="How long a browser action waits for its target before failing (default 10000).",
    ),
    Setting(
        key="video.fps", field="video_fps", group="Advanced", kind="int",
        label="Video FPS", description="Frames per second sampled from a recording for analysis.",
    ),
    Setting(
        key="video.duration", field="video_duration", group="Advanced", kind="str",
        label="Video duration (s)", description="Default desktop recording length, in seconds.",
    ),
    Setting(
        key="debug.dir", field="debug_dir", group="Advanced", kind="path",
        label="Debug / output dir",
        description="Where galaius writes logs + debug artifacts (blank = ~/.galaius/out; point at "
        "a project's out/ when working locally).",
    ),
]

_BY_KEY = {s.key: s for s in SETTINGS}


def by_key(key: str) -> Setting | None:
    return _BY_KEY.get(key)


def groups() -> list[tuple[str, list[Setting]]]:
    """Settings grouped in display order — ``[(group_name, [settings…]), …]``."""
    order: list[str] = []
    grouped: dict[str, list[Setting]] = {}
    for setting in SETTINGS:
        if setting.group not in grouped:
            grouped[setting.group] = []
            order.append(setting.group)
        grouped[setting.group].append(setting)
    return [(name, grouped[name]) for name in order]


def to_json_dict() -> dict:
    """Serialisable form for the VS Code extension — the STATIC field specs only (key, env,
    label, description, group, kind, role, enum options, default). Model dropdown options are
    deliberately NOT inlined: they depend on ``models.json`` and would couple this file to the
    catalog. The extension already bundles ``models.json`` and resolves a role's models itself
    (same ``role → capability`` rule), so this stays stable and changes only with the schema."""
    return {"settings": [setting.model_dump() for setting in SETTINGS]}


def _write_bundled() -> Path:
    """Regenerate the bundled ``galaius/data/settings.json`` from this schema (the extension
    build copies it in). Run ``python -m galaius.config.schema`` after editing SETTINGS."""
    import json
    from pathlib import Path

    # __file__ is galaius/config/schema.py → data/ is two levels up (the package root).
    target = Path(__file__).parent.parent / "data" / "settings.json"
    target.write_text(json.dumps(to_json_dict(), indent=2) + "\n")
    return target


if __name__ == "__main__":
    print("wrote", _write_bundled())
