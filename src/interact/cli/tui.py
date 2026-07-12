"""Terminal UI for interact — run bare ``interact`` to configure without typing commands.

Tabs: **Status** (environment + bindings + usage), **Connectors** (register the MCP server
with your tools — see what's connected, add more), **Config** (models + desktop target),
**API Keys** (set/clear the known provider keys, prefilled + masked), **Usage** (spend by
model and provider). Edits persist to ``~/.interact/config.env`` via :class:`UserConfig`
(the store the CLI reads and the extension mirrors) — that file lives in your home dir, not
the repo, so your keys are never committed.

Fully keyboard-driveable (Textual): ``Tab``/``Shift+Tab`` move focus, ``Ctrl+→``/``Ctrl+←``
switch tabs from anywhere, ``Enter``/``Space`` activate, plus the footer bindings. Mouse
works too. Heavy work (the model registry) loads in a background worker so the UI paints
instantly; the provider/usage details fill in a moment later.
"""

import os
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from interact.cli.clients import ClientTarget
from interact.config import SETTINGS, Setting, groups
from interact.config import UserConfig

# The three model roles (image/component/video) come from the shared schema — the Status tab
# shows the model resolved for each; the Config tab renders every setting from the same schema.
_MODEL_SETTINGS = [s for s in SETTINGS if s.kind == "model"]
_TAB_ORDER = ("tab-status", "tab-connectors", "tab-config", "tab-keys", "tab-usage")


def _mask(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:4]}…{value[-4:]}" if len(value) > 8 else "•" * len(value)


_AUTO = "\x00auto"  # Select sentinel for "no explicit value — use the default" (Select can't hold "")


def _field_id(setting: Setting) -> str:
    """A Textual-safe widget id for a setting (ids can't contain dots)."""
    return "set-" + setting.key.replace(".", "-")


def _select_options(setting: Setting) -> list[tuple[str, str]]:
    """(label, value) choices for a model/enum setting. Model dropdowns lead with ``(auto)``
    mapped to the _AUTO sentinel; enums use the schema's options as-is."""
    if setting.kind == "model":
        return [
            (opt.label, _AUTO if opt.value == "" else opt.value)
            for opt in setting.model_options()
        ]
    return [(opt.label, opt.value) for opt in (setting.options or [])]


def _available_model_ids() -> set[str] | None:
    """Model ids whose provider has a key set (config file or live env), from the bundled
    registry data — no litellm, no network. ``None`` means *no* provider has a key yet, the
    signal for the caller to show the whole catalogue so a first-run user can still browse."""
    from interact.data import PackageData

    providers = PackageData.models_data().get("providers", {})
    ids: set[str] = set()
    have_any = False
    for spec in providers.values():
        if any(UserConfig.get(k) or os.environ.get(k) for k in (spec.get("envKeys") or [])):
            have_any = True
            ids |= set((spec.get("models") or {}).keys())
    return ids if have_any else None


def _model_options(setting: Setting, configured: str) -> list[tuple[str, str]]:
    """Dropdown choices for a model setting: ``(auto)`` plus the models you can actually use
    (providers you have keys for), so the picker is short and relevant instead of a 45–128-item
    wall. With no keys yet, show the full catalogue to browse. A ``configured`` value that isn't
    in that set — a renamed/removed model, or a self-hosted/custom id — is ALWAYS appended so the
    Select can hold it without raising (the crash that made a stale config.env unopenable)."""
    full = _select_options(setting)
    available = _available_model_ids()
    options = list(full) if available is None else [
        (label, value) for label, value in full if value == _AUTO or value in available
    ]
    if configured and configured not in {value for _, value in options}:
        options.append((f"{configured}  ·  custom", configured))
    return options


def _select_value(setting: Setting, configured: str) -> str:
    """The value to show in a model/enum Select — GUARANTEED to be one of that widget's options,
    so assigning it (whether building the widget OR resetting it) can NEVER raise
    InvalidSelectValueError. A model keeps its configured id (``_model_options`` always offers it)
    or falls to ``(auto)``; an enum keeps a valid configured value, else its default, else the
    first option. One resolver for both build and reset, so the two can't drift apart."""
    if setting.kind == "model":
        return configured or _AUTO
    valid = [value for _, value in _select_options(setting)]  # enum: closed, ordered set
    if configured in valid:
        return configured
    return setting.default if setting.default in valid else (valid[0] if valid else _AUTO)


def _build_widget(setting: Setting):
    """Render the right Textual control for a setting's kind, pre-filled from the live config.
    Reads/writes by ``setting.env`` (the canonical INTERACT_* var) so the friendly key naming is
    free to match the VS Code extension's keys without affecting what's stored. Every Select's
    initial value comes from :func:`_select_value` — always one of its options — so a stale/custom
    persisted value can never raise InvalidSelectValueError and make the whole TUI fail to open."""
    configured = UserConfig.get(setting.env) or ""
    wid = _field_id(setting)
    if setting.kind == "model":
        return Select(_model_options(setting, configured), value=_select_value(setting, configured),
                      allow_blank=False, id=wid)
    if setting.kind == "enum":
        return Select(_select_options(setting), value=_select_value(setting, configured),
                      allow_blank=False, id=wid)
    if setting.kind == "bool":
        return Switch(value=(configured or setting.default).lower() == "true", id=wid)
    # int / str / path → text input; show the default as a placeholder, not a forced value.
    return Input(value=configured, placeholder=setting.default or "", id=wid)


def _known_key_names() -> list[str]:
    """Provider credential env-var names, from the bundled model registry data (not
    hardcoded), sorted alphabetically."""
    from interact.data import PackageData

    providers = PackageData.models_data().get("providers", {})
    return sorted({key for spec in providers.values() for key in (spec.get("envKeys") or [])})


def _key_state(name: str) -> str:
    """Masked current value of an env key, noting whether it comes from the config file or
    the live environment."""
    config_value = UserConfig.read().get(name)
    if config_value:
        return f"[green]{_mask(config_value)}[/green] [dim](config)[/dim]"
    env_value = os.environ.get(name)
    if env_value:
        return f"[green]{_mask(env_value)}[/green] [dim](environment)[/dim]"
    return "[dim]unset[/dim]"


class InteractTUI(App):
    """Configure interact interactively. Persists to ~/.interact/config.env."""

    TITLE = "interact"
    SUB_TITLE = "configure · connect · monitor"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { align: center top; }
    .field { height: auto; padding: 1 2 0 2; }
    .field .desc { margin-bottom: 1; }
    .group-heading { padding: 1 2 0 2; text-style: bold; color: $accent; }
    .row { height: auto; padding: 0 2; }
    .label { width: 24; content-align: left middle; }
    Input, Select { width: 56; }
    Button { min-width: 6; }
    .actions { height: auto; padding: 1 2 0 2; }
    .actions Button { margin: 0 2 0 0; }
    .conn-row { height: 3; align: left middle; }
    .conn-name { width: 22; content-align: left middle; padding: 0 1; }
    .conn-status { width: 1fr; content-align: left middle; }
    .conn-row Button { width: 11; margin: 0 1; }
    .key-row { height: 3; align: left middle; }
    .key-name { width: 26; content-align: left middle; padding: 0 1; }
    .key-state { width: 30; content-align: left middle; }
    .key-row Input { width: 1fr; }
    .key-row Button { width: 7; margin: 0 1; }
    #save-status { color: $success; padding: 1 2; }
    DataTable { height: auto; margin: 1 2; }
    Static.hint { color: $text-muted; padding: 1 2 0 2; }
    #update-banner { background: $warning; color: $text; padding: 0 2; text-style: bold; }
    .hidden { display: none; }
    """
    BINDINGS = [
        ("ctrl+s", "save_config", "Save config"),
        # priority=True so the tab chords fire even when a focused Input/Select would otherwise
        # swallow ctrl+arrow (word-nav) — the footer advertised these but they were dead in-terminal.
        Binding("ctrl+right", "next_tab", "Next tab", priority=True),
        Binding("ctrl+left", "prev_tab", "Prev tab", priority=True),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    # Filled by the background worker; placeholders show instantly on first paint.
    _models_info = "[dim]checking…[/dim]"
    _model_lines = tuple(f"  {s.role:<10} [dim]…[/dim]" for s in _MODEL_SETTINGS)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="update-banner", classes="hidden")
        with TabbedContent(initial="tab-status"):
            with TabPane("Status", id="tab-status"):
                yield VerticalScroll(Static("[dim]loading…[/dim]", id="status-body"))

            with TabPane("Connectors", id="tab-connectors"):
                with VerticalScroll():
                    yield Static(
                        "Register the interact MCP server with your AI tools. [green]✓[/green] = "
                        "connected. Use Install (Enter) to add one.",
                        classes="hint",
                    )
                    for target in ClientTarget.all():
                        with Horizontal(classes="conn-row"):
                            yield Label(target.label, classes="conn-name")
                            yield Static("", id=f"conn-{target.id}", classes="conn-status")
                            yield Button("Install", id=f"conn-install-{target.id}")

            with TabPane("Config", id="tab-config"):
                with VerticalScroll():
                    yield Static(
                        "Model menus list the providers you have keys for (add keys in API Keys) "
                        "— leave one on (auto) to let interact pick. Enter or Ctrl+S saves.",
                        classes="hint",
                    )
                    # Every field comes from the shared settings schema — the same spec the VS Code
                    # extension renders — so the two front ends never drift.
                    for group_name, settings in groups():
                        yield Label(f"[b]{group_name}[/b]", classes="group-heading")
                        for setting in settings:
                            yield from _field(
                                setting.label, setting.description, _build_widget(setting)
                            )
                    with Horizontal(classes="actions"):
                        yield Button("Save (Ctrl+S)", variant="primary", id="btn-save-config")
                        yield Button("Reset to defaults", id="btn-reset-config")
                    yield Static("", id="save-status")

            with TabPane("API Keys", id="tab-keys"):
                with VerticalScroll():
                    yield Static(
                        "Known provider keys (from the model registry), alphabetical. Stored in "
                        "~/.interact/config.env (chmod 600, in your home dir — never committed). "
                        "Existing values are prefilled and masked; Set saves, Clear removes.",
                        classes="hint",
                    )
                    for name in _known_key_names():
                        with Horizontal(classes="key-row"):
                            yield Label(name, classes="key-name")
                            yield Static(_key_state(name), id=f"state-{name}", classes="key-state")
                            yield Input(placeholder="paste to set…", password=True, id=f"in-{name}")
                            yield Button("Set", id=f"setkey-{name}")
                            yield Button("Clear", id=f"clearkey-{name}")

            with TabPane("Usage", id="tab-usage"):
                with VerticalScroll():
                    yield Static("", id="usage-summary", classes="hint")
                    yield Label("[b]By model[/b]")
                    yield DataTable(id="usage-table")
                    yield Label("[b]By provider[/b]")
                    yield DataTable(id="provider-table")
        yield Footer()

    def on_mount(self) -> None:
        # Everything here is light (file reads only) → instant first paint.
        self._refresh_connectors()
        self._refresh_usage_basic()
        self.query_one("#status-body", Static).update(self._status_text())
        self.run_worker(self._load_registry_info, thread=True, exclusive=True, group="registry")  # heavy bits, off-thread
        self.run_worker(self._check_update, thread=True)
        self._ensure_focus()  # so the keyboard works immediately, before any click

    def _ensure_focus(self) -> None:
        if self.focused is None:
            try:
                self.set_focus(self.query_one(TabbedContent))
            except Exception:
                pass

    def on_app_focus(self, event: events.AppFocus) -> None:
        # When the terminal regains focus (you clicked back into it), restore a focused
        # widget so the keyboard works again without having to click a panel first.
        self._ensure_focus()

    # ── tab navigation (works regardless of which widget has focus) ────────────
    def _switch_tab(self, delta: int) -> None:
        tabs = self.query_one(TabbedContent)
        index = (_TAB_ORDER.index(tabs.active) + delta) % len(_TAB_ORDER)
        tabs.active = _TAB_ORDER[index]

    def action_next_tab(self) -> None:
        self._switch_tab(1)

    def action_prev_tab(self) -> None:
        self._switch_tab(-1)

    def check_action(self, action: str, parameters):
        """Disable the bare ``q``/``r`` bindings while a form control is focused, so a fat-finger
        mid-config doesn't quit (losing unsaved edits) or refresh. Ctrl+C still quits from
        anywhere, and the tab chords (Ctrl+←/→) stay live so you can always reach a plain tab."""
        if action in ("quit", "refresh") and isinstance(self.focused, (Input, Select, Switch)):
            return False
        return True

    # ── Status (fast: no model-registry load) ──────────────────────────────────
    def _status_text(self) -> str:
        from interact.cli.usage import UsageReport

        cwd = Path(".").resolve()
        bound = [t.label for t in ClientTarget.all() if t.registrations(cwd)]
        report = UsageReport.build(since_days=30)
        return "\n".join([
            f"[b]Connected tools[/b] ({len(bound)}): "
            + (", ".join(bound) or "[dim]none — see the Connectors tab[/dim]"),
            f"[b]Providers with keys[/b]: {self._models_info}",
            "",
            "[b]Models[/b] (auto → the model picked for you):",
            *self._model_lines,
            f"  desktop    target={UserConfig.get('desktop.target') or 'local'}",
            "",
            f"[b]Usage[/b] (30 days): {report.entries} calls, ${report.total_cost:.4f}, "
            f"{report.total_input + report.total_output:,} tokens",
            f"[dim]Config: {UserConfig.PATH}[/dim]",
        ])

    def _load_registry_info(self) -> None:
        """Worker: load the registry off the UI thread, resolve providers + auto models +
        the by-provider usage breakdown, then update the panels. Fails soft."""
        from interact.cli.usage import UsageReport

        provider_rows: list[tuple[str, int, float]] = []
        try:
            from interact.models import Model, ModelCapability
            from interact.runtime import config

            Model.load_registry()
            available = set(Model.available_providers())
            grounding = len(Model.available_by_capability(ModelCapability.GUI_GROUNDING))
            self._models_info = (", ".join(sorted(available)) or "[red]none — see API Keys[/red]") \
                + f"  ·  {grounding} grounding-capable models"

            def auto(role: str) -> str:
                preferences = config.chain_for(role).preferences
                chosen = next((m.id for m in preferences if m.provider in available), None)
                return chosen or (preferences[0].id if preferences else "?")

            lines = []
            for setting in _MODEL_SETTINGS:
                configured = UserConfig.get(setting.env)
                lines.append(f"  {setting.role:<10} {configured}" if configured
                             else f"  {setting.role:<10} [dim]auto →[/dim] {auto(setting.role)}")
            self._model_lines = tuple(lines)

            def provider_of(name: str) -> str:
                model = Model.by_id(name)
                if model:
                    return model.provider
                if "/" in name:
                    return name.split("/", 1)[0]
                return next((m.provider for m in Model.registry() if m.id.endswith(f"/{name}")), "?")

            totals: dict[str, list] = {}
            for group in UsageReport.build().by_model:
                acc = totals.setdefault(provider_of(group.name), [0, 0.0])
                acc[0] += group.calls
                acc[1] += group.cost
            provider_rows = sorted(((p, c, cost) for p, (c, cost) in totals.items()),
                                   key=lambda r: r[2], reverse=True)
        except Exception as exc:
            self._models_info = f"[red]unavailable: {exc}[/red]"

        def apply() -> None:
            if not self.is_running:  # app torn down (e.g. test/quit) before the worker finished
                return
            try:
                self.query_one("#status-body", Static).update(self._status_text())
                table = self.query_one("#provider-table", DataTable)
                table.clear(columns=True)
                table.add_columns("provider", "calls", "cost")
                for provider, calls, cost in provider_rows:
                    table.add_row(provider, str(calls), f"${cost:.4f}")
            except NoMatches:
                pass  # widgets gone (closing) — nothing to update

        self.call_from_thread(apply)

    # ── Connectors ──────────────────────────────────────────────────────────────
    def _refresh_connectors(self) -> None:
        cwd = Path(".").resolve()
        for target in ClientTarget.all():
            try:
                registrations = target.registrations(cwd)
            except Exception:
                registrations = []
            self.query_one(f"#conn-{target.id}", Static).update(
                "[green]✓ " + ", ".join(registrations) + "[/green]" if registrations
                else "[dim]not connected[/dim]"
            )

    def _install_connector(self, client_id: str) -> None:
        from interact.cli.clients import MCPServer, Scope

        target = ClientTarget.by_id(client_id)
        if target is None:
            return
        server = MCPServer.resolve()
        cwd = Path(".").resolve()
        result = target.install(server, Scope.user, cwd, dry_run=False)
        if result.action == "skipped":  # no user scope (e.g. VS Code) → project
            result = target.install(server, Scope.project, cwd, dry_run=False)
        self.notify(f"{target.label}: {result.action} → {result.target}", timeout=6)
        self._refresh_connectors()
        self.query_one("#status-body", Static).update(self._status_text())

    # ── Config ────────────────────────────────────────────────────────────────
    def action_save_config(self) -> None:
        self._save_config()

    def _widget_value(self, setting: Setting) -> str:
        """Current on-screen value of a setting's widget, as the string we'd persist."""
        wid = f"#{_field_id(setting)}"
        if setting.kind in ("model", "enum"):
            return str(self.query_one(wid, Select).value)
        if setting.kind == "bool":
            return str(self.query_one(wid, Switch).value).lower()
        return self.query_one(wid, Input).value.strip()

    def _persist(self, setting: Setting, value: str) -> None:
        """Write a setting, but only when it differs from the default — so config.env holds just
        the user's overrides (``(auto)``/blank/the default all mean "unset")."""
        if value in ("", _AUTO) or value == setting.default:
            UserConfig.unset(setting.env)
        else:
            UserConfig.set(setting.env, value)

    def _save_config(self) -> None:
        for setting in SETTINGS:
            self._persist(setting, self._widget_value(setting))
        self.query_one("#save-status", Static).update("✓ saved to ~/.interact/config.env")
        # A toast too: the inline #save-status sits at the bottom of a scrolling pane and is easily
        # off-screen, so Ctrl+S / Save looked like it did nothing (it saved silently). Match the keys.
        self.notify("✓ Saved to ~/.interact/config.env")
        self.query_one("#status-body", Static).update(self._status_text())
        self.run_worker(self._load_registry_info, thread=True, exclusive=True, group="registry")  # auto-resolution may change

    def _reset_config(self) -> None:
        """Clear all persisted config settings and restore the on-screen defaults."""
        for setting in SETTINGS:
            UserConfig.unset(setting.env)
            wid = f"#{_field_id(setting)}"
            if setting.kind in ("model", "enum"):
                # via _select_value so a future enum whose default isn't a listed option can't
                # crash reset (the build path is guarded the same way).
                self.query_one(wid, Select).value = _select_value(setting, "")
            elif setting.kind == "bool":
                self.query_one(wid, Switch).value = setting.default.lower() == "true"
            else:
                self.query_one(wid, Input).value = ""
        self.query_one("#save-status", Static).update("✓ reset to defaults")
        self.notify("✓ Reset to defaults")
        self.query_one("#status-body", Static).update(self._status_text())

    def _rebuild_model_options(self) -> None:
        """Re-trim the model dropdowns after a key is set/cleared, so a newly-usable provider's
        models appear (and a de-keyed provider's drop) without restarting the TUI — the Config-tab
        hint promises exactly this. Each dropdown's current selection is preserved."""
        for setting in _MODEL_SETTINGS:
            try:
                select = self.query_one(f"#{_field_id(setting)}", Select)
            except NoMatches:
                continue
            current = str(select.value)
            select.set_options(_model_options(setting, "" if current == _AUTO else current))
            select.value = current  # always present: _model_options keeps (auto) + the current id

    # ── API keys (per-row set/clear of the known provider keys) ─────────────────
    def _set_key(self, name: str) -> None:
        value = self.query_one(f"#in-{name}", Input).value.strip()
        if not value:
            self.notify(f"paste a value for {name} first", severity="warning")
            return
        UserConfig.set(name, value)
        os.environ[name] = value  # live in THIS process now — config.env only seeds os.environ at
        # startup, so without this the status + model menus wouldn't reflect the key until restart
        self.query_one(f"#in-{name}", Input).value = ""
        self.query_one(f"#state-{name}", Static).update(_key_state(name))
        self._rebuild_model_options()  # this provider's models are now usable → show them
        self.notify(f"✓ set {name}")

    def _clear_key(self, name: str) -> None:
        removed = UserConfig.unset(name)
        if removed:
            # drop the copy apply() seeded into os.environ at startup, so the provider really
            # disappears from the status + menus — not just from config.env. A key that came from
            # the real shell env (removed=False) isn't ours to unset, so it's left untouched.
            os.environ.pop(name, None)
        self.query_one(f"#state-{name}", Static).update(_key_state(name))
        self._rebuild_model_options()  # provider no longer keyed → drop its models from the menus
        self.notify(f"cleared {name}" if removed else f"{name} was not in the config file")

    # ── Usage ────────────────────────────────────────────────────────────────
    def _refresh_usage_basic(self) -> None:
        from interact.cli.usage import UsageReport

        report = UsageReport.build()
        self.query_one("#usage-summary", Static).update(
            f"All-time: {report.entries} calls · ${report.total_cost:.4f} · "
            f"{report.total_input:,} input + {report.total_output:,} output tokens"
        )
        table = self.query_one("#usage-table", DataTable)
        table.clear(columns=True)
        table.add_columns("model", "calls", "tokens in", "tokens out", "cost")
        for group in report.by_model[:25]:
            table.add_row(group.name, str(group.calls), f"{group.input_tokens:,}",
                          f"{group.output_tokens:,}", f"${group.cost:.4f}")

    # ── Update banner ────────────────────────────────────────────────────────────
    def _check_update(self) -> None:
        from interact.cli.update import available_update

        newer = available_update()
        if not newer:
            return

        def show() -> None:
            if not self.is_running:
                return
            try:
                banner = self.query_one("#update-banner", Static)
                banner.update(f"  ⬆ Update available: v{newer} — quit and run `interact update`  ")
                banner.remove_class("hidden")
            except NoMatches:
                pass

        self.call_from_thread(show)

    def action_refresh(self) -> None:
        self._refresh_connectors()
        self._refresh_usage_basic()
        self.query_one("#status-body", Static).update(self._status_text())
        self.run_worker(self._load_registry_info, thread=True, exclusive=True, group="registry")
        self.notify("refreshed")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a text field acts, so keyboard users needn't tab to a button: an API-key
        row (``in-<NAME>``) saves that key; a Config field (``set-…``) saves the whole config."""
        input_id = event.input.id or ""
        if input_id.startswith("in-"):
            self._set_key(input_id.removeprefix("in-"))
        elif input_id.startswith("set-"):
            self._save_config()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "btn-save-config":
            self._save_config()
        elif button_id == "btn-reset-config":
            self._reset_config()
        elif button_id.startswith("conn-install-"):
            self._install_connector(button_id.removeprefix("conn-install-"))
        elif button_id.startswith("setkey-"):
            self._set_key(button_id.removeprefix("setkey-"))
        elif button_id.startswith("clearkey-"):
            self._clear_key(button_id.removeprefix("clearkey-"))


def _field(label: str, description: str, widget) -> ComposeResult:
    """A labelled control with a dim description (keyboard users can't hover). The description is
    a Static so it WRAPS instead of truncating on a narrow terminal (a Label clips at one line)."""
    with Vertical(classes="field"):
        yield Label(f"[b]{label}[/b]")
        yield Static(f"[dim]{description}[/dim]", classes="desc")
        yield widget


def run() -> None:
    InteractTUI().run()
