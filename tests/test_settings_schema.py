"""The shared settings schema is the single source of truth both front ends render from, so the
invariants that keep it honest matter: every setting maps to a real Config field (so its env var
and default can't drift — the bug that left the TUI writing INTERACT_BROWSER_HEADLESS that Config
never read), the bundled JSON the extension consumes stays in lock-step, and the TUI builds a
widget for every entry."""

import pytest

from interact.config import Config
from interact.data import PackageData
from interact.config import _ROLE_CAP, SETTINGS, by_key, groups, to_json_dict


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
    if setting.kind == "model":
        assert setting.role in _ROLE_CAP  # every model role declares a capability filter
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


def test_groups_cover_every_setting_in_order():
    flattened = [s for _, settings in groups() for s in settings]
    assert flattened == SETTINGS


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
