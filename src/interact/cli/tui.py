"""Terminal UI for interact — run bare ``interact`` to configure without typing commands.

Tabs: **Workspace** (server agent graph and navigation), **Status** (environment + bindings + usage), **Local tools** (register the MCP server
with your tools — see what's connected, add more), **Config** (models + desktop target),
**API Keys** (set/clear the known provider keys, prefilled + masked), **Usage** (spend by
model and provider). Local tool settings persist to ``~/.interact/config.env`` via
:class:`UserConfig`, shared with the CLI and extension. Workspace agent edits use
immutable server revisions and graph compare-and-swap.

Fully keyboard-driveable (Textual): ``Tab``/``Shift+Tab`` move focus, ``Ctrl+→``/``Ctrl+←``
switch tabs from anywhere, ``Enter``/``Space`` activate, plus the footer bindings. Mouse
works too. Heavy work (the model registry) loads in a background worker so the UI paints
instantly; the provider/usage details fill in a moment later.
"""

import asyncio
import json
import os
import time
import webbrowser
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from interact_core import AgentGraph, ConfiguredModelRef, WorkflowRevisionRef, WorkflowRun
from rich.text import Text

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
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
    Tree,
)

from interact.cli.clients import ClientTarget
from interact.cli.usage import UsageReport
from interact.config import SETTINGS, Setting, UserConfig, groups
from interact.server_tool_settings import PORTABLE_ENV, ServerToolSettings
from interact.upgrade.quiet import QuietPoint, UpgradeReady
from interact.upgrade.store import EXIT_UPGRADE
from interact.agents.catalog_connection import CatalogConnectionError
from interact.server_workspace import AgentEdit, ServerWorkspace, WorkflowConflictError, WorkflowRunRejected, WorkflowRunRequest

# Every role-carrying setting (image/component/video/audio criteria) comes from the shared
# schema — the Status tab shows what each one resolves to; the Config tab renders every setting
# (role or not) from the same schema, as a free-text criterion, not a fixed dropdown.
_MODEL_SETTINGS = [s for s in SETTINGS if s.role is not None]
_TAB_ORDER = ("tab-status", "tab-workspace", "tab-connectors", "tab-config", "tab-keys", "tab-usage")


class WorkspacePane(VerticalScroll):
    """Native controls over the same immutable server graph as the web editor."""

    DEFAULT_CSS = """
    WorkspacePane #workspace-tree { height: 12; min-height: 6; }
    WorkspacePane Input, WorkspacePane Select { width: 1fr; }
    WorkspacePane .workspace-actions { height: auto; layout: horizontal; }
    WorkspacePane .workspace-actions Button { margin-right: 1; }
    WorkspacePane Label { margin-top: 1; }
    """

    def __init__(self):
        super().__init__()
        self.server: ServerWorkspace | None = None
        self.graph: AgentGraph | None = None
        self.selected: UUID | None = None
        self.drafts: dict[UUID, AgentEdit] = {}
        self.busy = False
        self.current = False
        self.models: tuple[ConfiguredModelRef, ...] = ()
        self.tree_focus: UUID | None = None
        self.writable = False
        self.workflow_heads: dict[UUID, WorkflowRevisionRef] = {}
        self.workflow_requests: dict[UUID, WorkflowRunRequest] = {}
        self.workflow_settled: set[UUID] = set()
        self.workflow_running = False

    def compose(self):
        yield Static("Loading server workspace…", id="workspace-status", markup=False)
        with Horizontal(classes="workspace-actions"):
            yield Button("Reload / discard drafts", id="workspace-reload")
            yield Button("Assistant", id="workspace-link-assistant")
        yield Tree("Server agents", id="workspace-tree")
        with Horizontal(classes="workspace-actions"):
            yield Button("Focus subtree", id="workspace-subtree")
            yield Button("Parent subtree", id="workspace-up")
            yield Button("Whole tree", id="workspace-whole-tree")
        yield Static("Select an agent to inspect its exact revision.", id="workspace-agent", markup=False)
        yield Label("Reports to")
        yield Select([("No reporting parent", "")], id="workspace-parent", allow_blank=False)
        yield Label("Configured server model / provider connection")
        yield Select([("Resolve from criteria at launch", "")], id="workspace-model", allow_blank=False)
        yield Label("Model criteria")
        yield Input(id="workspace-criteria", placeholder="Server model eligibility expression")
        yield Label("Criteria weights")
        yield Input(id="workspace-weights")
        yield Label("Reasoning")
        yield Select([(value, value) for value in AgentEdit.model_fields["reasoning"].annotation.__args__],
                     id="workspace-reasoning", allow_blank=False, value="high")
        with Horizontal(classes="workspace-actions"):
            yield Button("Save agent", id="workspace-save", variant="primary", disabled=True)
            yield Button("Set Assistant root", id="workspace-root", disabled=True)
        yield Label("Server workflows")
        yield Select([("No workflows loaded", "")], id="workspace-workflow", allow_blank=False)
        yield Label("Workflow input values by name (JSON)")
        yield Input(value="{}", id="workspace-workflow-inputs")
        with Horizontal(classes="workspace-actions"):
            yield Button("Run selected revision", id="workspace-run-workflow", variant="primary")
            yield Button("Check run status", id="workspace-run-status")
            yield Button("New execution", id="workspace-new-execution")
        with Horizontal(classes="workspace-actions"):
            yield Button("Open selected workflow", id="workspace-open-workflow")
            yield Button("Execution history", id="workspace-workflow-history")
        yield Static("", id="workspace-workflows", markup=False)
        with Horizontal(classes="workspace-actions"):
            for route, label in (("agents", "Full graph"), ("workflows", "Workflows"), ("connections", "Providers")):
                yield Button(label, id=f"workspace-link-{route}")
        with Horizontal(classes="workspace-actions"):
            for route, label in (("company", "Company"), ("personal", "Account"), ("prompts", "Prompts"), ("version", "Version")):
                yield Button(label, id=f"workspace-link-{route}")

    def on_mount(self):
        self.run_worker(self.reload(), exclusive=True)

    def message(self, text: str):
        self.query_one("#workspace-status", Static).update(text)

    def remember(self):
        if self.selected is not None:
            parent = self.query_one("#workspace-parent", Select).value
            if parent is Select.NULL:
                return
            self.drafts[self.selected] = AgentEdit(
                reports_to=UUID(str(parent)) if parent else None,
                criteria=self.query_one("#workspace-criteria", Input).value.strip() or None,
                criteria_weights=self.query_one("#workspace-weights", Input).value,
                reasoning=self.query_one("#workspace-reasoning", Select).value,
                model=ConfiguredModelRef.model_validate_json(self.query_one("#workspace-model", Select).value)
                if self.query_one("#workspace-model", Select).value else None,
            )

    async def reload(self):
        if self.busy:
            return
        self.busy = True
        self.current = False
        self.message("Loading current server graph…")
        try:
            self.server = ServerWorkspace.configured()
            graph = await asyncio.to_thread(self.server.graph)
            self.writable = await asyncio.to_thread(self.server.can_edit)
            try:
                self.models = await asyncio.to_thread(self.server.models)
            except (ValueError, OSError, httpx.HTTPError):
                self.models = ()
            self.drafts.clear()
            self.selected = None
            self.accept(graph)
            try:
                workflows = await asyncio.to_thread(self.server.workflows)
                self.workflow_heads = {workflow.key.id: WorkflowRevisionRef(key=workflow.key, revision=workflow.revision) for workflow in workflows}
                self.query_one("#workspace-workflow", Select).set_options(
                    [(f"{item.name} · {item.revision}", str(item.key.id)) for item in workflows] or [("No workflows", "")])
                self.query_one("#workspace-workflows", Static).update("Run uses the displayed revision and supplied inputs. Server checks existing execute permission. Retries reuse the same request key.")
            except (ValueError, OSError, httpx.HTTPError):
                self.query_one("#workspace-workflows", Static).update("Workflow list unavailable. Open Workflows to inspect permissions and retry.")
        except (ValueError, OSError, httpx.HTTPError) as error:
            self.message(str(error) if isinstance(error, CatalogConnectionError) else "Server unavailable; draft retained. No offline writes.")
            self.query_one("#workspace-save", Button).disabled = True
            self.query_one("#workspace-root", Button).disabled = True
        finally:
            self.busy = False

    def accept(self, graph: AgentGraph, *, verified: bool = True):
        self.current = verified
        self.graph = graph
        tree = self.query_one("#workspace-tree", Tree)
        tree.clear()
        focus = next((agent for agent in graph.agents if agent.id == self.tree_focus), None)
        tree.root.set_label(Text(focus.name if focus else "Assistant root / reporting tree"))
        tree.root.data = focus.id if focus else None
        children: dict[UUID | None, list] = {}
        ids = {agent.id for agent in graph.agents}
        for agent in graph.agents:
            parent = agent.reports_to if agent.reports_to in ids else None
            children.setdefault(parent, []).append(agent)
        # Textual's renderer recursively traverses expanded branches. Bound each view's
        # depth; Focus subtree keeps arbitrarily deep server hierarchies reachable.
        pending = [(tree.root, focus.id if focus else None, 0)]
        visited = set()
        while pending:
            node, parent, depth = pending.pop()
            for agent in children.get(parent, []):
                if agent.id in visited:
                    continue
                visited.add(agent.id)
                root = " [Assistant root]" if graph.root_agent and graph.root_agent.id == agent.id else ""
                more = " [focus subtree for descendants]" if depth == 31 and children.get(agent.id) else ""
                child = node.add(Text(f"{agent.name}{root}{more}"), data=agent.id, expand=depth < 3)
                if depth < 31:
                    pending.append((child, agent.id, depth + 1))
        tree.root.expand()
        self.message(f"{'Current server' if verified else 'Last viewed; server unavailable'}: {self.server.connection.endpoint} · workspace {self.server.connection.workspace_id} · {len(graph.agents)} agents" +
                     (" · read-only access" if not self.writable else ""))
        if self.selected in ids:
            self.inspect(self.selected)

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        if event.node.data is not None and not self.busy:
            self.remember()
            self.inspect(event.node.data)

    def inspect(self, agent_id: UUID):
        agent = next(agent for agent in self.graph.agents if agent.id == agent_id)
        self.selected = agent_id
        draft = self.drafts.get(agent_id) or AgentEdit(reports_to=agent.reports_to, criteria=agent.criteria,
                                                       criteria_weights=agent.criteria_weights, reasoning=agent.reasoning, model=agent.model)
        self.query_one("#workspace-agent", Static).update(f"{agent.name} · {agent.role_key or 'unnamed role'}\nRevision {agent.revision}\nModel: {agent.model.id if agent.model else 'resolved from criteria at launch'} · {len(agent.capabilities)} capabilities")
        parent = self.query_one("#workspace-parent", Select)
        parent.set_options([("No reporting parent", "")] + [(item.name, str(item.id)) for item in self.graph.agents if item.id != agent.id])
        parent.value = str(draft.reports_to) if draft.reports_to else ""
        models = {model.model_dump_json(): model for model in self.models}
        if draft.model is not None:
            models[draft.model.model_dump_json()] = draft.model
        model_select = self.query_one("#workspace-model", Select)
        model_select.set_options([("Resolve from criteria at launch", "")] +
                                 [(f"{model.id} · connection {model.connection.id}", key) for key, model in models.items()])
        model_select.value = draft.model.model_dump_json() if draft.model else ""
        self.query_one("#workspace-criteria", Input).value = draft.criteria or ""
        self.query_one("#workspace-weights", Input).value = draft.criteria_weights
        self.query_one("#workspace-reasoning", Select).value = draft.reasoning
        for action in ("save", "root"):
            self.query_one(f"#workspace-{action}", Button).disabled = not self.current or not self.writable

    async def save(self, root: bool = False):
        if self.busy or self.selected is None or self.graph is None or self.server is None:
            return
        self.remember()
        self.busy = True
        self.message("Saving server graph…")
        try:
            saved = await asyncio.to_thread(self.server.set_root, self.graph, self.selected) if root else await asyncio.to_thread(
                self.server.edit_agent, self.graph, self.selected, self.drafts[self.selected])
            if not root:
                self.drafts.pop(self.selected, None)
            elif self.selected in self.drafts:
                self.drafts[self.selected] = self.drafts[self.selected].model_copy(update={"reports_to": None})
            self.accept(saved)
        except (ValueError, OSError, httpx.HTTPError) as error:
            self.message(str(error) if isinstance(error, CatalogConnectionError) else "Invalid edit or server unavailable; draft retained. Check criteria and reporting parent.")
        finally:
            self.busy = False

    async def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        button = event.button.id or ""
        if button == "workspace-reload":
            await self.reload()
        elif button in {"workspace-run-workflow", "workspace-run-status", "workspace-new-execution"}:
            await self.workflow_action(button)
        elif button in {"workspace-save", "workspace-root"}:
            await self.save(root=button == "workspace-root")
        elif button in {"workspace-subtree", "workspace-up", "workspace-whole-tree"} and self.graph:
            self.remember()
            self.tree_focus = self.selected if button == "workspace-subtree" else None
            if button == "workspace-up" and self.selected:
                self.tree_focus = next(agent.reports_to for agent in self.graph.agents if agent.id == self.selected)
            self.accept(self.graph, verified=self.current)
        elif button in {"workspace-open-workflow", "workspace-workflow-history"} and self.server:
            identity = self.query_one("#workspace-workflow", Select).value
            if not identity or identity is Select.NULL:
                self.message("Select a server workflow first.")
            elif button == "workspace-open-workflow":
                await asyncio.to_thread(webbrowser.open, self.server.link("workflows", UUID(str(identity))))
            else:
                try:
                    runs = await asyncio.to_thread(self.server.workflow_history, UUID(str(identity)))
                    self.query_one("#workspace-workflows", Static).update("\n".join(f"{run.status} · {run.id} · revision {run.workflow.revision}" for run in runs) or "No executions yet.")
                except (ValueError, OSError, httpx.HTTPError):
                    self.query_one("#workspace-workflows", Static).update("Execution history unavailable. Check server access and retry.")
        elif button.startswith("workspace-link-") and self.server is not None:
            route = button.removeprefix("workspace-link-")
            await asyncio.to_thread(webbrowser.open, self.server.link(route, self.selected if route == "agents" else None))

    def show_workflow_run(self, run: WorkflowRun):
        identity = run.workflow.key.id
        if run.status in {"succeeded", "failed", "cancelled", "interrupted"}:
            self.workflow_settled.add(identity)
        else:
            self.workflow_settled.discard(identity)
        self.query_one("#workspace-workflows", Static).update(
            f"{run.status} · run {run.id}\nWorkflow {identity} · revision {run.workflow.revision}\n"
            f"Updated {run.updated_at.isoformat()} · request key {run.idempotency_key}")

    async def workflow_action(self, action: str):
        if self.workflow_running or self.server is None:
            return
        selected = self.query_one("#workspace-workflow", Select).value
        status = self.query_one("#workspace-workflows", Static)
        if selected is Select.NULL or not selected:
            status.update("Select a server workflow first.")
            return
        identity = UUID(str(selected))
        request = self.workflow_requests.get(identity)
        retry = request is not None
        can_confirm_rejection = not retry or identity in self.workflow_settled
        if action == "workspace-new-execution":
            if request and identity not in self.workflow_settled:
                status.update("Check the existing execution first. New execution requires a final status or confirmed rejection without an earlier uncertain attempt.")
                return
            self.workflow_requests.pop(identity, None)
            self.workflow_settled.discard(identity)
            status.update("New execution prepared. Review selected revision and input values, then Run selected revision.")
            return
        self.workflow_running = True
        self.query_one("#workspace-run-workflow", Button).disabled = True
        try:
            if action == "workspace-run-status":
                if request is None:
                    status.update("No request submitted in this session. Execution history lists earlier runs.")
                    return
                run = await asyncio.to_thread(self.server.workflow_status, identity, request.idempotency_key)
                if run is None:
                    status.update("Request already resolved. Review inputs and choose New execution." if identity in self.workflow_settled else
                                  f"Request not found yet. Retry with the same key {request.idempotency_key}; do not create a second execution.")
                else:
                    self.show_workflow_run(run)
                return
            reference = self.workflow_heads.get(identity)
            if reference is None:
                status.update("Reload workflows before executing; no displayed revision is available.")
                return
            proposed = WorkflowRunRequest(workflow=reference, idempotency_key=request.idempotency_key if request else str(uuid4()),
                                          invocation={"values": json.loads(self.query_one("#workspace-workflow-inputs", Input).value)})
            if request is not None and proposed != request:
                status.update("Revision or inputs differ from the retained request. Use New execution after confirmed rejection or final status; an uncertain request must retain its inputs and key.")
                return
            request = proposed
            self.workflow_requests[identity] = request
            self.workflow_settled.discard(identity)
            status.update(f"Submitting workflow {identity}\nRevision {reference.revision} · request key {request.idempotency_key}")
            self.show_workflow_run(await asyncio.to_thread(self.server.run_workflow, request))
        except (WorkflowConflictError, WorkflowRunRejected) as error:
            if can_confirm_rejection:
                self.workflow_settled.add(identity)
            status.update(str(error) + (" An earlier attempt may still exist; check its status before New execution." if retry and identity not in self.workflow_settled else ""))
        except (ValueError, OSError, httpx.HTTPError) as error:
            status.update(str(error) if isinstance(error, CatalogConnectionError) else "Invalid input values or unavailable server. Request retained; check status before retrying.")
        finally:
            self.workflow_running = False
            self.query_one("#workspace-run-workflow", Button).disabled = False


def _provider_usage_rows(report: UsageReport) -> list[tuple[str, int, float, int]]:
    """Rows from persisted provider identity; model display names are not provider ids."""
    return [
        (group.name, group.calls, group.cost, group.unknown_cost_calls)
        for group in report.by_provider
    ]


def _mask(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:4]}…{value[-4:]}" if len(value) > 8 else "•" * len(value)


_AUTO = "\x00auto"  # Select sentinel for "no explicit value — use the default" (Select can't hold "")


def _field_id(setting: Setting) -> str:
    """A Textual-safe widget id for a setting (ids can't contain dots)."""
    return "set-" + setting.key.replace(".", "-")


def _select_options(setting: Setting) -> list[tuple[str, str]]:
    """(label, value) choices for an enum setting."""
    return [(opt.label, opt.value) for opt in (setting.options or [])]


def _select_value(setting: Setting, configured: str) -> str:
    """The value to show in an enum Select — GUARANTEED to be one of that widget's options, so
    assigning it (whether building the widget OR resetting it) can NEVER raise
    InvalidSelectValueError. A valid configured value is kept, else the default, else the first
    option. One resolver for both build and reset, so the two can't drift apart."""
    valid = [value for _, value in _select_options(setting)]  # closed, ordered set
    if configured in valid:
        return configured
    return setting.default if setting.default in valid else (valid[0] if valid else _AUTO)


def _build_widget(setting: Setting, values: dict[str, str] | None = None):
    """Render the right Textual control for a setting's kind, pre-filled from the live config.
    Reads/writes by ``setting.env`` (the canonical INTERACT_* var) so the friendly key naming is
    free to match the VS Code extension's keys without affecting what's stored. A role's own
    requirement (image/component/video/audio criteria) is kind="str" like any other free-text
    setting — a criterion is a sentence somebody writes and edits, not a choice from a dropdown
    of ids. Every Select's initial value comes from :func:`_select_value` — always one of its
    options — so a stale/custom persisted value can never raise InvalidSelectValueError and make
    the whole TUI fail to open."""
    configured = (values.get(setting.env) if values is not None else UserConfig.get(setting.env)) or ""
    wid = _field_id(setting)
    if setting.kind == "enum":
        return Select(_select_options(setting), value=_select_value(setting, configured),
                      allow_blank=False, id=wid)
    if setting.kind == "bool":
        return Switch(value=(configured or setting.default).lower() == "true", id=wid)
    # int / str / path (incl. every role's criterion) → text input; default shown as a
    # placeholder, not a forced value.
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
        ("ctrl+s", "save_config", "Save"),
        # priority=True so tab chords fire even when a focused Input/Select would otherwise
        # swallow ctrl+arrow (word-nav) — footer advertised these but they were dead in-terminal.
        Binding("ctrl+right", "next_tab", "Next tab", priority=True),
        Binding("ctrl+left", "prev_tab", "Prev tab", priority=True),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    # Filled by the background worker; placeholders show instantly on first paint.
    _models_info = "[dim]checking…[/dim]"
    _model_lines = tuple(f"  {s.role:<10} [dim]…[/dim]" for s in _MODEL_SETTINGS)

    def compose(self) -> ComposeResult:
        self._settings_snapshot = None
        self._settings_data = UserConfig.read_local()
        self._settings_message = "Local settings"
        server = ServerToolSettings.configured()
        if server:
            self._settings_data = {key: value for key, value in self._settings_data.items() if key not in PORTABLE_ENV}
            try:
                self._settings_snapshot = server.read()
                self._settings_data.update(self._settings_snapshot.env)
                self._settings_message = self._settings_source()
            except (ValueError, OSError) as error:
                self._settings_message = str(error)
        yield Header(show_clock=True)
        yield Static("", id="update-banner", classes="hidden")
        with TabbedContent(initial="tab-status"):
            with TabPane("Status", id="tab-status"):
                yield VerticalScroll(Static("[dim]loading…[/dim]", id="status-body"))

            with TabPane("Workspace", id="tab-workspace"):
                yield WorkspacePane()

            with TabPane("Local tools", id="tab-connectors"):
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

            with TabPane("Tool settings", id="tab-config"):
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
                                setting.label, setting.description, _build_widget(setting, self._settings_data)
                            )
                    with Horizontal(classes="actions"):
                        yield Button("Save (Ctrl+S)", variant="primary", id="btn-save-config")
                        yield Button("Reset to defaults", id="btn-reset-config")
                        yield Button("Reload server", id="btn-reload-config")
                    yield Static(self._settings_message, id="save-status")

            with TabPane("Local API keys", id="tab-keys"):
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
        self._quiet = QuietPoint.current()
        self._last_input = time.monotonic()
        if self._quiet is not None:
            self.set_interval(5, self._upgrade_when_idle)
        self._ensure_focus()  # so the keyboard works immediately, before any click

    def _ensure_focus(self) -> None:
        if self.focused is None:
            try:
                self.set_focus(self.query_one(TabbedContent))
            except Exception:
                pass

    def on_app_focus(self, event: events.AppFocus) -> None:
        # When the terminal regains focus (clicked back into it), restore a focused widget so
        # the keyboard works again without clicking a panel first.
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
        provider_rows: list[tuple[str, int, float, int]] = []
        try:
            from interact.models import Model, ModelCapability
            from interact.runtime import config

            Model.load_registry()
            available = set(Model.available_providers())
            grounding = len(Model.available_by_capability(ModelCapability.GUI_GROUNDING))
            self._models_info = (", ".join(sorted(available)) or "[red]none — see API Keys[/red]") \
                + f"  ·  {grounding} grounding-capable models"

            def resolved_line(role: str) -> str:
                """Show what the role's criterion RESOLVES to, never the criterion text alone —
                a blank where an answer belongs is the defect this line exists to prevent."""
                criteria_text = config.criteria_for(role)
                try:
                    chosen = config.resolve_model(role)
                except RuntimeError:
                    return f"  {role:<10} [red]nothing resolves[/red]  [dim]({criteria_text})[/dim]"
                return f"  {role:<10} {chosen}  [dim]({criteria_text})[/dim]"

            self._model_lines = tuple(
                resolved_line(setting.role) for setting in _MODEL_SETTINGS if setting.role
            )

            provider_rows = _provider_usage_rows(UsageReport.build())
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
                for provider, calls, cost, unknown in provider_rows:
                    shown = (
                        f"${cost:.4f} + unknown"
                        if unknown and cost
                        else "unknown"
                        if unknown
                        else f"${cost:.4f}"
                    )
                    table.add_row(provider, str(calls), shown)
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
        if self.query_one(TabbedContent).active == "tab-workspace":
            self.run_worker(self.query_one(WorkspacePane).save(), exclusive=True, group="workspace-save")
            return
        self._save_config()

    def _widget_value(self, setting: Setting) -> str:
        """Current on-screen value of a setting's widget, as the string we'd persist."""
        wid = f"#{_field_id(setting)}"
        if setting.kind == "enum":
            return str(self.query_one(wid, Select).value)
        if setting.kind == "bool":
            return str(self.query_one(wid, Switch).value).lower()
        return self.query_one(wid, Input).value.strip()

    def _save_config(self) -> None:
        changes = {}
        for setting in SETTINGS:
            value = self._widget_value(setting)
            changes[setting.env] = None if value in ("", _AUTO, setting.default) else value
        if UserConfig.server() is None and self._settings_snapshot is None:
            UserConfig.update(changes)
            self._settings_data = UserConfig.read_local()
            self.query_one("#save-status", Static).update("Saved local settings")
            self.notify("Saved local settings")
            return
        self.run_worker(self._save_settings(changes), group="settings-save", exclusive=False)

    def _settings_source(self) -> str:
        snapshot = self._settings_snapshot
        return (f"Personal server settings · revision {snapshot.settings.revision} · "
                f"{'STALE cache; reload before saving' if snapshot.stale else 'current'} · machine settings and keys stay local") if snapshot else "Local settings"

    async def _save_settings(self, changes: dict[str, str | None], *, reset: bool = False) -> None:
        if getattr(self, "_settings_saving", False):
            return
        self._settings_saving = True
        try:
            if UserConfig.server() and self._settings_snapshot is None:
                raise ValueError("Reload personal settings before saving. Draft retained.")
            saved = await asyncio.to_thread(UserConfig.update, changes, base=self._settings_snapshot)
            self._settings_snapshot = saved
            self._settings_data = {key: value for key, value in self._settings_data.items() if key not in changes}
            self._settings_data.update({key: value for key, value in changes.items() if value is not None})
            if reset:
                self._reset_settings_widgets()
            self.query_one("#save-status", Static).update("Saved · " + self._settings_source())
            self.notify("Saved personal settings")
        except (ValueError, OSError) as error:
            self.query_one("#save-status", Static).update(str(error) + " Draft retained.")
        finally:
            self._settings_saving = False

    async def _reload_settings(self) -> None:
        if getattr(self, "_settings_saving", False):
            return
        self._settings_saving = True
        try:
            server = UserConfig.server()
            snapshot = await asyncio.to_thread(server.read, allow_stale=False) if server else None
            self._settings_snapshot = snapshot
            self._settings_data = UserConfig.read_local()
            if snapshot:
                self._settings_data = {key: value for key, value in self._settings_data.items() if key not in PORTABLE_ENV}
                self._settings_data.update(snapshot.env)
            for setting in SETTINGS:
                widget = self.query_one(f"#{_field_id(setting)}")
                value = self._settings_data.get(setting.env, "")
                if isinstance(widget, Select):
                    widget.value = _select_value(setting, value)
                elif isinstance(widget, Switch):
                    widget.value = (value or setting.default).lower() == "true"
                else:
                    widget.value = value
            self.query_one("#save-status", Static).update(self._settings_source())
        except (ValueError, OSError) as error:
            self.query_one("#save-status", Static).update(str(error))
        finally:
            self._settings_saving = False

    def _reset_config(self) -> None:
        """Clear all persisted config settings and restore the on-screen defaults."""
        changes = {setting.env: None for setting in SETTINGS}
        if UserConfig.server() is None and self._settings_snapshot is None:
            UserConfig.update(changes)
            self._settings_data = UserConfig.read_local()
            self.query_one("#save-status", Static).update("Reset local settings to defaults")
            self.notify("Reset local settings to defaults")
            self._reset_settings_widgets()
        else:
            self.run_worker(self._save_settings(changes, reset=True), group="settings-save")

    def _reset_settings_widgets(self) -> None:
        for setting in SETTINGS:
            wid = f"#{_field_id(setting)}"
            if setting.kind == "enum":
                # via _select_value so a future enum whose default isn't a listed option can't
                # crash reset (the build path is guarded the same way).
                self.query_one(wid, Select).value = _select_value(setting, "")
            elif setting.kind == "bool":
                self.query_one(wid, Switch).value = setting.default.lower() == "true"
            else:
                self.query_one(wid, Input).value = ""
        self.query_one("#status-body", Static).update(self._status_text())

    # ── API keys (per-row set/clear of the known provider keys) ─────────────────
    def _set_key(self, name: str) -> None:
        value = self.query_one(f"#in-{name}", Input).value.strip()
        if not value:
            self.notify(f"paste a value for {name} first", severity="warning")
            return
        UserConfig.set(name, value)
        os.environ[name] = value  # live in THIS process now — config.env only seeds os.environ at
        # startup; without this, status/model menus wouldn't reflect the key until restart
        self.query_one(f"#in-{name}", Input).value = ""
        self.query_one(f"#state-{name}", Static).update(_key_state(name))
        self.notify(f"✓ set {name}")

    def _clear_key(self, name: str) -> None:
        removed = UserConfig.unset(name)
        if removed:
            # Drop the copy apply() seeded into os.environ at startup, so the provider really
            # disappears from status/menus, not just config.env. A key from the real shell env
            # (removed=False) isn't ours to unset — left untouched.
            os.environ.pop(name, None)
        self.query_one(f"#state-{name}", Static).update(_key_state(name))
        self.notify(f"cleared {name}" if removed else f"{name} was not in the config file")

    # ── Usage ────────────────────────────────────────────────────────────────
    def _refresh_usage_basic(self) -> None:
        report = UsageReport.build()
        session_note = (
            f" · {report.session_usage_calls} session calls (account impact unknown)"
            if report.session_usage_calls
            else ""
        )
        self.query_one("#usage-summary", Static).update(
            f"All-time: {report.entries} calls · observed metered API spend "
            f"${report.total_cost:.4f}{session_note} · "
            f"{report.total_input:,} input + {report.total_output:,} output tokens"
        )
        table = self.query_one("#usage-table", DataTable)
        table.clear(columns=True)
        table.add_columns("model", "calls", "tokens in", "tokens out", "cost")
        for group in report.by_model[:25]:
            shown = (
                f"${group.cost:.4f} + unknown"
                if group.unknown_cost_calls and group.cost
                else "unknown"
                if group.unknown_cost_calls
                else f"${group.cost:.4f}"
            )
            table.add_row(
                group.name,
                str(group.calls),
                f"{group.input_tokens:,}",
                f"{group.output_tokens:,}",
                shown,
            )

    # ── Upgrade ──────────────────────────────────────────────────────────────────
    #: Without a key press or click this long, a waiting upgrade restarts the dashboard on it.
    upgrade_idle_seconds = 60.0

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, (events.Key, events.MouseDown, events.MouseScrollDown, events.MouseScrollUp)):
            self._last_input = time.monotonic()
        await super().on_event(event)

    def _upgrade_when_idle(self) -> None:
        """Say that a new version waits; leave for it once nobody has touched the dashboard."""
        if self._quiet is None or not self._quiet.waiting():
            return
        banner = self.query_one("#update-banner", Static)
        banner.update(f"  New Interact version ready: this dashboard restarts on it after {self.upgrade_idle_seconds:.0f} s without a key press  ")
        banner.remove_class("hidden")
        try:
            self._quiet.leave_if_quiet(time.monotonic() - self._last_input >= self.upgrade_idle_seconds)
        except UpgradeReady:
            self.exit(return_code=EXIT_UPGRADE)

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
        elif button_id == "btn-reload-config":
            self.run_worker(self._reload_settings(), group="settings-reload", exclusive=True)
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
    app = InteractTUI()
    app.run()
    if app.return_code == EXIT_UPGRADE:
        raise UpgradeReady("the dashboard left for a new version")
