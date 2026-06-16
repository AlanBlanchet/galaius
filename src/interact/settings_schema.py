"""The one declarative description of interact's user-configurable settings.

Every front end that lets a user configure interact — the bare-``interact`` Textual TUI and the
VS Code extension panel — renders from THIS list, instead of each re-declaring the fields, labels,
defaults and env-var mappings (which had already drifted: the TUI wrote ``INTERACT_BROWSER_HEADLESS``
that :class:`~interact.config.Config` never reads, and TUI/extension disagreed on ``debug.dir``).

A :class:`Setting` is keyed to a real ``Config`` attribute (``field``), so its env-var name and
default are derived from the runtime config and can't drift; a test asserts every setting maps to
an existing field. The schema is exported to JSON (``PackageData.settings_raw``) for the extension,
which generates its env-map and renders its Configuration panel from the same source.

Front ends consume the common spec and override only presentation when they must (a richer widget,
hiding a field) — the *behaviour* (which key, which env var, the default) stays shared.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, computed_field

from interact.config import Config

SettingKind = Literal["model", "enum", "bool", "int", "str", "path"]
SettingGroup = Literal["Models", "Desktop", "Browser", "Advanced"]

# Capability a model must have to appear in a role's dropdown (image/component ground UI elements;
# video lists any vision model — the registry has no separate video capability flag).
_ROLE_CAP = {"image": "gui_grounding", "component": "gui_grounding", "video": "vlm"}


class Option(BaseModel):
    """One choice for an ``enum`` setting (or a model dropdown): a human label + the stored value."""

    label: str
    value: str


class Setting(BaseModel):
    """A single user-configurable setting, keyed to a ``Config`` attribute so its env-var name and
    default come from the runtime config (no second copy to keep in sync)."""

    key: str  # friendly dotted key used by UserConfig / the extension, e.g. "image.model"
    field: str  # the Config attribute this maps to, e.g. "image_model" — source of env + default
    label: str
    description: str
    group: SettingGroup
    kind: SettingKind
    role: str | None = None  # for kind="model": the model role (image/component/video)
    options: list[Option] | None = None  # for kind="enum"

    @computed_field
    @property
    def env(self) -> str:
        """The environment variable Config reads for this setting (its single source of truth)."""
        return f"INTERACT_{self.field.upper()}"

    @computed_field
    @property
    def default(self) -> str:
        """The field's *declared* default as a string — from the Config field definition, NOT a
        live ``Config()`` instance (which would fold in the current INTERACT_* env and make the
        exported JSON depend on the environment it was generated in). Empty string = "auto /
        unset"; home is collapsed to ``~`` so the export is portable, not the build machine's path."""
        from pathlib import Path

        value = Config.model_fields[self.field].default
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        if isinstance(value, Path):
            # Render path defaults POSIX-style (forward slashes) so the exported JSON is byte-identical
            # on every OS — otherwise Windows bakes `~\.interact` and drifts from the bundled (Linux-
            # generated) settings.json, failing the lockstep check.
            text, home = value.as_posix(), Path.home().as_posix()
        else:
            text, home = str(value), str(Path.home())
        return "~" + text[len(home):] if text.startswith(home) else text

    def model_options(self) -> list[Option]:
        """For a ``model`` setting: ``(auto)`` first, then bundled model ids capable of the role.
        Reads the bundled catalog only — no network, no registry load."""
        from interact.data import PackageData

        cap = _ROLE_CAP.get(self.role or "", "gui_grounding")
        ids: set[str] = set()
        for spec in PackageData.models_data().get("providers", {}).values():
            for model_id, mspec in (spec.get("models") or {}).items():
                if cap in (mspec.get("capabilities") or []):
                    ids.add(model_id)
        return [Option(label="(auto — best available)", value="")] + [
            Option(label=mid, value=mid) for mid in sorted(ids)
        ]


# Ordered by group; the order here is the order shown in every front end.
SETTINGS: list[Setting] = [
    # ── Models ───────────────────────────────────────────────────────────────
    Setting(
        key="image.model", field="image_model", group="Models", kind="model", role="image",
        label="Vision model",
        description="Screenshots & media analysis (the default for most VLM calls).",
    ),
    Setting(
        key="component.model", field="component_model", group="Models", kind="model", role="component",
        label="Component model",
        description="UI-element detection / GUI grounding (falls back to the vision model).",
    ),
    Setting(
        key="video.model", field="video_model", group="Models", kind="model", role="video",
        label="Video model",
        description="Video understanding (needs a model with native video support).",
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
    # ── Advanced ─────────────────────────────────────────────────────────────
    Setting(
        key="vlm.maxTokens", field="max_tokens", group="Advanced", kind="int",
        label="VLM max tokens",
        description="Cap on VLM output tokens per call (blank = the model's default).",
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
        description="Where interact writes logs + debug artifacts (blank = ~/.interact; point at a "
        "project's out/ when working locally).",
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
    """Regenerate the bundled ``interact/data/settings.json`` from this schema (the extension
    build copies it in). Run ``python -m interact.settings_schema`` after editing SETTINGS."""
    import json
    from pathlib import Path

    target = Path(__file__).parent / "data" / "settings.json"
    target.write_text(json.dumps(to_json_dict(), indent=2) + "\n")
    return target


if __name__ == "__main__":
    print("wrote", _write_bundled())
