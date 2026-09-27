"""The shared settings schema is the single source of truth both front ends render from, so the
invariants that keep it honest matter: every setting maps to a real Config field (so its env var
and default can't drift — the bug that left the TUI writing INTERACT_BROWSER_HEADLESS that Config
never read), the bundled JSON the extension consumes stays in lock-step, and the TUI builds a
widget for every entry."""

import json

import pytest
from pathlib import Path
from pydantic import ValidationError

from interact.config import Config
from interact.data import PackageData
from interact.config import SETTINGS, by_key, groups, to_json_dict


def test_every_setting_maps_to_a_real_config_field():
    """The whole point: env + default come from a live Config attribute, so a typo'd or stale
    field is caught here instead of silently writing an env var the server ignores."""
    fields = set(Config.model_fields)
    assert {s.field for s in SETTINGS} <= fields, [s.key for s in SETTINGS if s.field not in fields]


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s.key)
def test_env_is_derived_from_the_field(setting):
    assert setting.env == f"INTERACT_{setting.field.upper()}"


def test_browser_headless_reaches_the_field_config_reads():
    """Regression for the drift bug: browser.headless must write INTERACT_HEADLESS (the var
    Config.headless reads), not INTERACT_BROWSER_HEADLESS."""
    assert by_key("browser.headless").env == "INTERACT_HEADLESS"


def test_keys_are_unique():
    keys = [s.key for s in SETTINGS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s.key)
def test_kind_specific_shape(setting):
    if setting.role is not None:
        assert setting.kind == "str"  # a criterion is free text, never a fixed dropdown of ids
    if setting.kind == "enum":
        assert setting.options, f"{setting.key} is an enum with no options"
    if setting.kind != "enum":
        assert setting.options is None


def test_default_collapses_home_so_export_is_portable():
    """A path default must not bake the build machine's absolute home into the bundled JSON."""
    debug = by_key("debug.dir")
    assert debug.default in ("", "~/.interact") or debug.default.startswith("~/")


def test_bundled_settings_json_is_in_lockstep_with_the_schema():
    """The extension reads the bundled settings.json; if SETTINGS changed without regenerating
    (`python -m interact.config.schema`), this fails — the same staleness guard as models.json."""
    assert PackageData.settings_data() == to_json_dict()


def test_nested_default_and_numeric_constraints_have_one_declarative_owner() -> None:
    nested = by_key("desktop.nestedHeadless")
    width = by_key("browser.viewportWidth")
    height = by_key("browser.viewportHeight")
    assert nested is not None and nested.default == "true"
    assert width is not None and width.minimum == 1
    assert height is not None and height.minimum == 1
    assert by_key("browser.slowMo").minimum is None
    package = json.loads((Path(__file__).parent.parent / "clients" / "vscode" / "package.json").read_text())
    properties = package["contributes"]["configuration"]["properties"]
    assert properties["interact.desktop.nestedHeadless"]["default"] is True
    assert properties["interact.desktop.nestedSize"]["pattern"] == r"^[1-9]\d*x[1-9]\d*$"
    size = by_key("desktop.nestedSize")
    assert size.pattern == r"^[1-9]\d*x[1-9]\d*$"
    for exported in (
        Path("src/interact/data/settings.json"),
        Path("clients/vscode/src/settings.json"),
    ):
        projected = next(item for item in json.loads(exported.read_text())["settings"]
                         if item["key"] == "desktop.nestedSize")
        assert projected["pattern"] == size.pattern
    assert properties["interact.browser.viewportWidth"]["minimum"] == 1
    assert properties["interact.browser.viewportHeight"]["minimum"] == 1


@pytest.mark.parametrize("field,key", [
    ("viewport_width", "browser.viewportWidth"),
    ("viewport_height", "browser.viewportHeight"),
])
def test_viewport_minimum_is_owned_by_config_and_projected_everywhere(field: str, key: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Config(**{field: 0})
    runtime = Config.model_json_schema()["properties"][field]
    setting = by_key(key)
    assert runtime["minimum"] == 1
    assert setting.minimum == runtime["minimum"]
    for exported in (PackageData.settings_data(), json.loads(Path("clients/vscode/src/settings.json").read_text())):
        projected = next(item for item in exported["settings"] if item["key"] == key)
        assert projected["minimum"] == runtime["minimum"]


def test_unconstrained_integer_domain_still_accepts_negative_values() -> None:
    assert Config(slow_mo=-2).slow_mo == -2
    assert by_key("browser.slowMo").minimum is None


@pytest.mark.parametrize("value", ["", "garbage", "1280", "1280x", "0x800", "1280x-1"])
def test_nested_size_rejects_values_outside_the_canonical_positive_geometry(value: str) -> None:
    with pytest.raises(ValidationError, match="nested_size"):
        Config(nested_size=value)


def test_session_media_guidance_is_claude_only() -> None:
    root = Path(__file__).parent.parent
    root_section = (root / "README.md").read_text().split("### Models and keys", 1)[1].split("\n## ", 1)[0]
    extension_section = (root / "clients" / "vscode" / "README.md").read_text().split(
        "## Visual sessions, billing, and models", 1
    )[1].split("\n## ", 1)[0]
    confirmation = by_key("media.noExtraUsageConfirmedFor")
    bundled = PackageData.settings_data()["settings"]
    bundled_confirmation = next(
        setting for setting in bundled if setting["key"] == confirmation.key
    )
    for text in (root_section, extension_section, confirmation.description, bundled_confirmation["description"]):
        assert "codex" not in text.lower()
        assert "openai" not in text.lower()


def test_groups_cover_every_setting_in_order():
    flattened = [s for _, settings in groups() for s in settings]
    assert flattened == SETTINGS


@pytest.mark.parametrize(
    ("key", "word"),
    [("image.criteria", "requirement"), ("criteria.weights", "normalized")],
)
def test_media_selection_policy_is_editable_and_unit_safe(key: str, word: str) -> None:
    setting = by_key(key)
    assert setting.group == "Models" and setting.kind == "str"
    assert word in setting.description.lower()


@pytest.mark.asyncio
async def test_tui_renders_a_widget_for_every_setting(tmp_path, monkeypatch):
    """The TUI's Config tab is generated from the schema — every setting must yield a control."""
    from interact.config import UserConfig

    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")  # never touch the real file
    from interact.cli.tui import InteractTUI, _field_id

    app = InteractTUI()
    async with app.run_test() as pilot:
        app.query_one("TabbedContent").active = "tab-config"
        await pilot.pause()
        missing = [s.key for s in SETTINGS if not app.query(f"#{_field_id(s)}")]
        assert not missing, missing
