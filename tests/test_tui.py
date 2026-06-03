"""The configuration TUI (bare `interact`) — driven headlessly via Textual's Pilot."""

import pytest
from textual.widgets import Input, Select, Static, Switch, TabbedContent

from interact.tui import InteractTUI, _mask
from interact.userconfig import UserConfig


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")
    return UserConfig.PATH


@pytest.mark.parametrize(
    "value,expected",
    [("sk-abcd1234efgh", "sk-a…efgh"), ("short", "•••••"), ("", "")],
)
def test_mask(value, expected):
    assert _mask(value) == expected


async def test_tui_saves_config(temp_config):
    # All panes' widgets are always in the DOM (TabbedContent keeps them mounted), so we
    # set values and invoke the save handler directly — no dependence on tab-switch timing.
    app = InteractTUI()
    async with app.run_test():
        app.query_one("#in-image", Input).value = "gpt-4o"
        app.query_one("#sel-target", Select).value = "nested"
        app.query_one("#sw-headless", Switch).value = True
        app.query_one("#in-debug-dir", Input).value = "/tmp/x/out"
        app._save_config()

    data = UserConfig.read()
    assert data["INTERACT_IMAGE_MODEL"] == "gpt-4o"
    assert data["INTERACT_DESKTOP_TARGET"] == "nested"
    assert data["INTERACT_NESTED_HEADLESS"] == "true"
    assert data["INTERACT_DEBUG_DIR"] == "/tmp/x/out"


async def test_tui_reset_to_defaults(temp_config):
    UserConfig.set("image.model", "gpt-4o")
    UserConfig.set("desktop.target", "nested")
    UserConfig.set("debug.dir", "/x/out")
    app = InteractTUI()
    async with app.run_test():
        app._reset_config()
        assert app.query_one("#in-image", Input).value == ""
        assert app.query_one("#sel-target", Select).value == "local"
    data = UserConfig.read()
    assert "INTERACT_IMAGE_MODEL" not in data
    assert "INTERACT_DESKTOP_TARGET" not in data
    assert "INTERACT_DEBUG_DIR" not in data


async def test_tui_save_clears_emptied_model(temp_config):
    UserConfig.set("image.model", "gpt-4o")
    app = InteractTUI()
    async with app.run_test():
        app.query_one("#in-image", Input).value = ""  # cleared → unset
        app._save_config()
    assert "INTERACT_IMAGE_MODEL" not in UserConfig.read()


def test_known_key_names_from_registry_sorted():
    """Key names come from the bundled registry data (not hardcoded) and are alphabetical."""
    from interact.tui import _known_key_names

    names = _known_key_names()
    assert names == sorted(names)
    assert "OPENAI_API_KEY" in names and "GEMINI_API_KEY" in names


async def test_tui_set_and_clear_known_key(temp_config):
    app = InteractTUI()
    async with app.run_test():
        app.query_one("#in-OPENAI_API_KEY", Input).value = "sk-secret-value-123"
        app._set_key("OPENAI_API_KEY")
        assert UserConfig.read()["OPENAI_API_KEY"] == "sk-secret-value-123"
        assert app.query_one("#in-OPENAI_API_KEY", Input).value == ""  # cleared from screen
        app._clear_key("OPENAI_API_KEY")
        assert "OPENAI_API_KEY" not in UserConfig.read()


async def test_tui_key_dispatch_via_button(temp_config):
    """Per-row Set button routes to the handler (covers on_button_pressed)."""
    app = InteractTUI()
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-keys"
        await pilot.pause()
        # ANTHROPIC_API_KEY sorts first, so its row is on-screen for a real click.
        app.query_one("#in-ANTHROPIC_API_KEY", Input).value = "ak-12345678"
        app.query_one("#setkey-ANTHROPIC_API_KEY").scroll_visible()
        await pilot.pause()
        await pilot.click("#setkey-ANTHROPIC_API_KEY")
        await pilot.pause()
    assert UserConfig.read()["ANTHROPIC_API_KEY"] == "ak-12345678"


async def test_tui_prefills_existing_key_masked(temp_config, monkeypatch):
    """A key present in the environment shows up masked (prefilled), never in clear text."""
    from textual.widgets import Static as _Static

    monkeypatch.setenv("GEMINI_API_KEY", "gm-abcd1234wxyz")
    app = InteractTUI()
    async with app.run_test():
        state = str(app.query_one("#state-GEMINI_API_KEY", _Static).render())
    assert "gm-a…wxyz" in state and "environment" in state
    assert "gm-abcd1234wxyz" not in state  # full secret never rendered


async def test_tui_mounts_all_tabs(temp_config):
    """Smoke: the app builds every tab without error (they read clients, config, usage)."""
    app = InteractTUI()
    async with app.run_test():
        for tab in ("tab-status", "tab-connectors", "tab-config", "tab-keys", "tab-usage"):
            app.query_one(TabbedContent).active = tab


async def test_tui_lists_all_connectors_with_install_buttons(temp_config):
    """The Connectors tab shows every known client with an Install button — so the user
    can add VS Code, Cursor, Codex, … not just whatever happens to be registered."""
    from textual.widgets import Button

    from interact.clients import ClientTarget

    app = InteractTUI()
    async with app.run_test():
        for target in ClientTarget.all():
            app.query_one(f"#conn-install-{target.id}", Button)  # raises if missing
            app.query_one(f"#conn-{target.id}")  # status cell present


async def test_tui_shows_update_banner_when_newer_release(temp_config, monkeypatch):
    from interact import update as update_mod

    monkeypatch.setattr(update_mod, "available_update", lambda *a, **k: "9.9.9")
    app = InteractTUI()
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        banner = app.query_one("#update-banner", Static)
        assert "9.9.9" in str(banner.render())
        assert not banner.has_class("hidden")


async def test_tui_fields_have_descriptions(temp_config):
    """Each config field carries a human description (keyboard users can't hover)."""
    app = InteractTUI()
    async with app.run_test():
        descriptions = " ".join(str(label.render()) for label in app.query("Label.desc"))
        assert "GUI grounding" in descriptions
        assert "isolated sandbox" in descriptions
