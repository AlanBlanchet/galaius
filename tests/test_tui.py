"""The configuration TUI (bare `interact`) — driven headlessly via Textual's Pilot."""

import pytest
from textual.widgets import Input, Select, Static, Switch, TabbedContent

from interact.config import by_key
from interact.cli.tui import InteractTUI, _field_id, _mask
from interact.config import UserConfig


def _sid(key: str) -> str:
    """The TUI widget id for a setting (rendered from the shared schema)."""
    return "#" + _field_id(by_key(key))


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


# A real grounding-capable id present in the model dropdown (so Select accepts it).
_PICK = "gemini/gemini-3.5-flash"


async def test_tui_saves_config(temp_config):
    # All panes' widgets are always in the DOM (TabbedContent keeps them mounted), so we
    # set values and invoke the save handler directly — no dependence on tab-switch timing.
    app = InteractTUI()
    async with app.run_test():
        app.query_one(_sid("image.model"), Select).value = _PICK  # pick from the dropdown
        app.query_one(_sid("desktop.target"), Select).value = "nested"
        app.query_one(_sid("desktop.nestedHeadless"), Switch).value = True
        app.query_one(_sid("debug.dir"), Input).value = "/tmp/x/out"
        app._save_config()

    data = UserConfig.read()
    assert data["INTERACT_IMAGE_MODEL"] == _PICK
    assert data["INTERACT_DESKTOP_TARGET"] == "nested"
    assert data["INTERACT_NESTED_HEADLESS"] == "true"
    assert data["INTERACT_DEBUG_DIR"] == "/tmp/x/out"


async def test_tui_reset_to_defaults(temp_config):
    from interact.cli.tui import _AUTO

    UserConfig.set("image.model", _PICK)
    UserConfig.set("desktop.target", "nested")
    UserConfig.set("debug.dir", "/x/out")
    app = InteractTUI()
    async with app.run_test():
        app._reset_config()
        assert app.query_one(_sid("image.model"), Select).value == _AUTO  # back to "(auto)"
        assert app.query_one(_sid("desktop.target"), Select).value == "local"
    data = UserConfig.read()
    assert "INTERACT_IMAGE_MODEL" not in data
    assert "INTERACT_DESKTOP_TARGET" not in data
    assert "INTERACT_DEBUG_DIR" not in data


async def test_tui_save_auto_unsets_model(temp_config):
    # Selecting "(auto)" must remove the persisted model (the clear-doesn't-save bug fix).
    from interact.cli.tui import _AUTO

    UserConfig.set("image.model", _PICK)
    app = InteractTUI()
    async with app.run_test():
        app.query_one(_sid("image.model"), Select).value = _AUTO  # → unset
        app._save_config()
    assert "INTERACT_IMAGE_MODEL" not in UserConfig.read()


async def test_tui_survives_stale_or_custom_model(temp_config):
    """A persisted model id absent from the current registry — renamed/removed upstream, or a
    self-hosted/custom id the user typed into config.env — must NOT crash the TUI at compose
    (it used to raise InvalidSelectValueError). The value stays selected + selectable."""
    UserConfig.set("image.model", "my-local/llava-custom")
    app = InteractTUI()
    async with app.run_test():  # previously: InvalidSelectValueError → whole TUI unopenable
        sel = app.query_one(_sid("image.model"), Select)
        assert sel.value == "my-local/llava-custom"


async def test_tui_survives_invalid_enum(temp_config):
    """An invalid persisted enum value falls back to the setting's default, never crashes."""
    UserConfig.set("desktop.target", "wayland-nonsense")
    app = InteractTUI()
    async with app.run_test():
        sel = app.query_one(_sid("desktop.target"), Select)
        assert sel.value == by_key("desktop.target").default


def test_model_options_trim_and_keep(monkeypatch):
    """Model dropdowns list only providers you have keys for (+ auto + any configured value),
    so the picker isn't a 128-item wall; with NO keys they show the full list to browse."""
    from interact.cli import tui

    s = by_key("image.model")
    full = tui._select_options(s)

    monkeypatch.setattr(tui, "_available_model_ids", lambda: None)  # no keys → browse everything
    assert tui._model_options(s, "") == full

    some = {v for _, v in full if v.startswith("gemini/")}
    assert some and some != {v for _, v in full}  # a real strict subset exists to trim to
    monkeypatch.setattr(tui, "_available_model_ids", lambda: set(some))
    opts = tui._model_options(s, "")
    vals = {v for _, v in opts}
    assert tui._AUTO in vals  # (auto) always offered
    assert vals - {tui._AUTO} <= some  # nothing from an un-keyed provider
    assert len(opts) < len(full)  # actually trimmed

    kept = tui._model_options(s, "weird/custom-x")  # a configured value survives the trim
    assert "weird/custom-x" in {v for _, v in kept}


async def test_tui_enter_in_key_input_sets_it(temp_config):
    """Enter in an API-key field saves that key (no need to tab to the Set button)."""
    from textual.widgets import Input as _Input

    app = InteractTUI()
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-keys"
        await pilot.pause()
        field = app.query_one("#in-OPENAI_API_KEY", _Input)
        field.value = "sk-entered-by-return"
        field.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert UserConfig.read()["OPENAI_API_KEY"] == "sk-entered-by-return"


async def test_tui_enter_in_config_input_saves(temp_config):
    """Enter in a Config text field persists the whole config (Enter == Ctrl+S there)."""
    from textual.widgets import Input as _Input

    app = InteractTUI()
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        field = app.query_one(_sid("debug.dir"), _Input)
        field.value = "/tmp/entered/out"
        field.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert UserConfig.read()["INTERACT_DEBUG_DIR"] == "/tmp/entered/out"


def test_select_value_always_in_its_options():
    """The shared resolver never returns a value outside the Select's options — so building OR
    resetting a Select can't raise InvalidSelectValueError, even for a stale enum value."""
    from interact.cli import tui

    enum = by_key("desktop.target")
    valid = {v for _, v in tui._select_options(enum)}
    assert tui._select_value(enum, "wayland-stale") == enum.default  # invalid → default
    assert tui._select_value(enum, "wayland-stale") in valid
    assert tui._select_value(enum, "nested") == "nested"  # valid kept
    model = by_key("image.model")
    assert tui._select_value(model, "any/custom-x") == "any/custom-x"  # custom kept
    assert tui._select_value(model, "") == tui._AUTO  # blank → (auto)


async def test_rebuild_model_options_preserves_selection(temp_config):
    """Rebuilding the model dropdowns (after a key change) keeps the current selection and never
    raises — the value is guaranteed to survive the re-trim."""
    from interact.cli.tui import _AUTO

    app = InteractTUI()
    async with app.run_test():
        sel = app.query_one(_sid("image.model"), Select)
        sel.value = _AUTO
        app._rebuild_model_options()
        assert sel.value == _AUTO


async def test_set_and_clear_key_refresh_model_dropdowns(temp_config):
    """Setting or clearing a provider key re-trims the model menus (finding: they used to go
    stale until a full TUI restart, though the Config hint promised otherwise)."""
    app = InteractTUI()
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-keys"
        await pilot.pause()
        calls = []
        app._rebuild_model_options = lambda: calls.append(True)
        app.query_one("#in-GEMINI_API_KEY", Input).value = "gm-test"
        app._set_key("GEMINI_API_KEY")
        app._clear_key("GEMINI_API_KEY")
        assert len(calls) == 2  # once per set, once per clear


async def test_tui_save_and_reset_show_a_toast(temp_config):
    """Save / Reset surface a visible toast — the inline #save-status sits at the bottom of a
    scrolling pane and is easily off-screen, which made Ctrl+S look like it did nothing."""
    app = InteractTUI()
    async with app.run_test():
        toasts = []
        app.notify = lambda msg, **kw: toasts.append(msg)
        app._save_config()
        app._reset_config()
    assert any("Saved" in t for t in toasts)
    assert any("Reset" in t for t in toasts)


async def test_set_and_clear_key_update_process_env(temp_config, monkeypatch):
    """Setting/clearing a key takes effect in THIS process's os.environ live — so the status +
    model menus reflect it without a restart, and a cleared key really disappears (it doesn't
    linger in os.environ, seeded there by apply() at startup, and keep the provider looking keyed)."""
    import os as _os

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    app = InteractTUI()
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-keys"
        await pilot.pause()
        try:
            app.query_one("#in-MISTRAL_API_KEY", Input).value = "ms-live-value"
            app._set_key("MISTRAL_API_KEY")
            assert _os.environ.get("MISTRAL_API_KEY") == "ms-live-value"  # live at once
            app._clear_key("MISTRAL_API_KEY")
            assert "MISTRAL_API_KEY" not in _os.environ  # cleared live, not just in config.env
        finally:
            _os.environ.pop("MISTRAL_API_KEY", None)


async def test_tui_quit_disabled_while_editing_a_control(temp_config):
    """Bare q/r must not fire while a form control is focused (fat-finger mid-config would quit
    and lose unsaved edits); they stay live elsewhere, and unrelated actions are never touched."""
    app = InteractTUI()
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        app.query_one(_sid("image.model"), Select).focus()
        await pilot.pause()
        assert app.check_action("quit", ()) is False
        assert app.check_action("refresh", ()) is False
        app.set_focus(None)
        assert app.check_action("quit", ()) is True
        assert app.check_action("save_config", ()) is True  # unrelated binding never disabled


def test_known_key_names_from_registry_sorted():
    """Key names come from the bundled registry data (not hardcoded) and are alphabetical."""
    from interact.cli.tui import _known_key_names

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

    from interact.cli.clients import ClientTarget

    app = InteractTUI()
    async with app.run_test():
        for target in ClientTarget.all():
            app.query_one(f"#conn-install-{target.id}", Button)  # raises if missing
            app.query_one(f"#conn-{target.id}")  # status cell present


async def test_tui_shows_update_banner_when_newer_release(temp_config, monkeypatch):
    from interact.cli import update as update_mod

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
        descriptions = " ".join(str(label.render()) for label in app.query(".desc"))
        assert "GUI grounding" in descriptions
        assert "isolated sandbox" in descriptions
