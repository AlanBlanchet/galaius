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

from interact.clients import ClientTarget
from interact.userconfig import UserConfig

_MODEL_ROLES = (
    ("image", "Vision model for screenshots and media analysis"),
    ("component", "UI-element detection / GUI grounding (falls back to the image model)"),
    ("video", "Video understanding (needs native video support)"),
)
_TAB_ORDER = ("tab-status", "tab-connectors", "tab-config", "tab-keys", "tab-usage")


def _mask(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:4]}…{value[-4:]}" if len(value) > 8 else "•" * len(value)


_AUTO = "\x00auto"  # Select sentinel for "no explicit model — use auto" (Select can't hold "")


def _model_options(role: str) -> list[tuple[str, str]]:
    """Dropdown choices for a model role: ``(auto)`` first, then the bundled model ids capable
    of this role (grounding for image/component, video for video) — so a user picks from a list
    instead of typing ids by hand. Reads bundled JSON only; no network, no registry load."""
    from interact.data import PackageData

    data = PackageData.models_data()
    # image/component need grounding; video lists any vision model (the registry tags vlm, no
    # separate video capability). Falls back to vlm so the list is never empty for a role.
    cap = "vlm" if role == "video" else "gui_grounding"
    ids: set[str] = set()
    for spec in data.get("providers", {}).values():
        for model_id, mspec in (spec.get("models") or {}).items():
            if cap in (mspec.get("capabilities") or []):
                ids.add(model_id)
    options = [("(auto — best available)", _AUTO)]
    options += [(mid, mid) for mid in sorted(ids)]
    return options


def _model_select_value(role: str) -> str:
    configured = UserConfig.get(f"{role}.model")
    return configured if configured else _AUTO


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
        ("ctrl+right", "next_tab", "Next tab"),
        ("ctrl+left", "prev_tab", "Prev tab"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    # Filled by the background worker; placeholders show instantly on first paint.
    _models_info = "[dim]checking…[/dim]"
    _model_lines = tuple(f"  {role:<10} [dim]…[/dim]" for role, _ in _MODEL_ROLES)

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
                    for role, description in _MODEL_ROLES:
                        yield from _field(
                            f"{role} model", description,
                            Select(_model_options(role), value=_model_select_value(role),
                                   allow_blank=False, id=f"in-{role}"),
                        )
                    yield from _field(
                        "desktop target",
                        "Where desktop automation acts: your real session, or an isolated sandbox",
                        Select([("local — your real session", "local"),
                                ("nested — isolated sandbox display", "nested")],
                               value=UserConfig.get("desktop.target") or "local",
                               allow_blank=False, id="sel-target"),
                    )
                    yield from _field(
                        "nested headless",
                        "Sandbox only: ON = Xvfb in the background (CI/servers); OFF = Xephyr you can watch",
                        Switch(value=(UserConfig.get("nested.headless") or "").lower() == "true", id="sw-headless"),
                    )
                    yield from _field(
                        "nested display", "X display number for the sandbox (e.g. 99 → :99)",
                        Input(value=UserConfig.get("nested.display") or "99", id="in-nested-display"),
                    )
                    yield from _field(
                        "nested size", "Sandbox screen size, WIDTHxHEIGHT",
                        Input(value=UserConfig.get("nested.size") or "1280x800", id="in-nested-size"),
                    )
                    yield from _field(
                        "browser headless", "Run the automation browser without a visible window",
                        Switch(value=(UserConfig.get("browser.headless") or "true").lower() == "true",
                               id="sw-browser-headless"),
                    )
                    yield from _field(
                        "debug dir",
                        "Where interact writes its logs + debug artifacts (default ~/.interact; "
                        "point at a project's out/ when working locally)",
                        Input(value=UserConfig.get("debug.dir") or "", placeholder="~/.interact",
                              id="in-debug-dir"),
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
        self.run_worker(self._load_registry_info, thread=True)  # heavy bits, off-thread
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

    # ── Status (fast: no model-registry load) ──────────────────────────────────
    def _status_text(self) -> str:
        from interact.usage import UsageReport

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
        from interact.usage import UsageReport

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
            for role, _ in _MODEL_ROLES:
                configured = UserConfig.get(f"{role}.model")
                lines.append(f"  {role:<10} {configured}" if configured
                             else f"  {role:<10} [dim]auto →[/dim] {auto(role)}")
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
        from interact.clients import MCPServer, Scope

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

    def _save_config(self) -> None:
        for role, _ in _MODEL_ROLES:
            value = self.query_one(f"#in-{role}", Select).value
            UserConfig.set(f"{role}.model", value) if value and value != _AUTO else UserConfig.unset(f"{role}.model")
        UserConfig.set("desktop.target", self.query_one("#sel-target", Select).value)
        UserConfig.set("nested.headless", str(self.query_one("#sw-headless", Switch).value).lower())
        UserConfig.set("nested.display", self.query_one("#in-nested-display", Input).value.strip() or "99")
        UserConfig.set("nested.size", self.query_one("#in-nested-size", Input).value.strip() or "1280x800")
        UserConfig.set("browser.headless", str(self.query_one("#sw-browser-headless", Switch).value).lower())
        debug_dir = self.query_one("#in-debug-dir", Input).value.strip()
        UserConfig.set("debug.dir", debug_dir) if debug_dir else UserConfig.unset("debug.dir")
        self.query_one("#save-status", Static).update("✓ saved to ~/.interact/config.env")
        self.query_one("#status-body", Static).update(self._status_text())
        self.run_worker(self._load_registry_info, thread=True)  # auto-resolution may change

    def _reset_config(self) -> None:
        """Clear all persisted config settings and restore the on-screen defaults."""
        for role, _ in _MODEL_ROLES:
            UserConfig.unset(f"{role}.model")
            self.query_one(f"#in-{role}", Select).value = _AUTO
        for key in ("desktop.target", "nested.headless", "nested.display", "nested.size",
                    "browser.headless", "debug.dir"):
            UserConfig.unset(key)
        self.query_one("#sel-target", Select).value = "local"
        self.query_one("#sw-headless", Switch).value = False
        self.query_one("#in-nested-display", Input).value = "99"
        self.query_one("#in-nested-size", Input).value = "1280x800"
        self.query_one("#sw-browser-headless", Switch).value = True
        self.query_one("#in-debug-dir", Input).value = ""
        self.query_one("#save-status", Static).update("✓ reset to defaults")
        self.query_one("#status-body", Static).update(self._status_text())

    # ── API keys (per-row set/clear of the known provider keys) ─────────────────
    def _set_key(self, name: str) -> None:
        value = self.query_one(f"#in-{name}", Input).value.strip()
        if not value:
            self.notify(f"paste a value for {name} first", severity="warning")
            return
        UserConfig.set(name, value)
        self.query_one(f"#in-{name}", Input).value = ""
        self.query_one(f"#state-{name}", Static).update(_key_state(name))
        self.notify(f"✓ set {name}")

    def _clear_key(self, name: str) -> None:
        removed = UserConfig.unset(name)
        self.query_one(f"#state-{name}", Static).update(_key_state(name))
        self.notify(f"cleared {name}" if removed else f"{name} was not in the config file")

    # ── Usage ────────────────────────────────────────────────────────────────
    def _refresh_usage_basic(self) -> None:
        from interact.usage import UsageReport

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
        from interact.update import available_update

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
        self.run_worker(self._load_registry_info, thread=True)
        self.notify("refreshed")

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
    """A labelled control with a dim one-line description (keyboard users can't hover)."""
    with Vertical(classes="field"):
        yield Label(f"[b]{label}[/b]")
        yield Label(f"[dim]{description}[/dim]", classes="desc")
        yield widget


def run() -> None:
    InteractTUI().run()
