/** Native workspace controls; Python owns authentication, validation and graph CAS. */
import * as vscode from "vscode";
import { interactCli } from "./interactCli.ts";
import { type PolicyRule } from "./agentModels.ts";
import { workspaceCommand, refreshWorkspace } from "./serverWorkspace.ts";
import { acceptWorkspace, parseWorkspace, parseWorkspaceModel, type WorkspaceAgent, type WorkspaceView, type WorkspaceModel } from "./workspaceState.ts";
import { WorkflowExecution, type WorkflowSelection } from "./workflowExecution.ts";

interface AgentDraft {
  revision: string;
  edit: { criteria?: string | null; criteria_weights?: string; reasoning?: string; reports_to?: string | null; model?: WorkspaceModel | null };
}

export class ServerWorkspaceControls implements vscode.Disposable {
  private drafts = new Map<string, AgentDraft>();
  private executions = new Map<string, WorkflowExecution>();
  private status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 15);
  constructor(private readonly changed: () => void) {
    this.status.command = "interact.workspace";
    this.status.name = "Interact server workspace";
    this.status.text = "$(cloud) Workspace: loading";
    this.status.show();
  }
  dispose(): void { this.status.dispose(); }
  async refresh(): Promise<WorkspaceView | null> {
    try {
      const view = await refreshWorkspace();
      const root = view?.graph.agents.find(agent => agent.id === view.graph.root_agent?.id);
      this.status.text = view ? `$(cloud) ${root?.name ?? "No Assistant root"}` : "$(plug) Workspace: not connected";
      this.status.tooltip = view ? `${view.origin}\nWorkspace ${view.workspace_id}\nCurrent server graph · ${view.writable ? "editable" : "read-only"}` : "Local tool settings remain available. Connect using interact agents sync.";
      this.changed();
      return view;
    } catch (error) {
      this.status.text = "$(warning) Workspace: unavailable";
      this.status.tooltip = String(error);
      this.changed();
      throw error;
    }
  }
  async open(named?: string): Promise<void> {
    try {
      const view = await vscode.window.withProgress({ location: vscode.ProgressLocation.Window, title: "Read server workspace" }, () => this.refresh());
      if (!view) { void vscode.window.showInformationMessage("Connect a workspace with interact agents sync. Local tool settings remain local."); return; }
      if (named) {
        const agent = view.graph.agents.find(item => item.role_key === named || item.id === named);
        if (!agent) throw new Error("Agent is absent from the current server graph.");
        await this.editAgent(view, agent);
        return;
      }
      const action = await vscode.window.showQuickPick([
        { label: "Agents and Assistant root", route: "graph", description: `${view.graph.agents.length} current server agents` },
        { label: "Workflows", route: "workflows" }, { label: "Assistant", route: "assistant" },
        { label: "Provider connections", route: "connections" }, { label: "Company", route: "company" },
        { label: "Account", route: "personal" }, { label: "Prompts and paradigms", route: "prompts" },
        { label: "Version and changelog", route: "version" },
      ], { title: `Server workspace · ${view.origin}`, placeHolder: view.writable ? "Current server records" : "Read-only connection; edit in the signed-in server app" });
      if (!action) return;
      if (action.route === "graph") {
        const agent = await vscode.window.showQuickPick(view.graph.agents.map(item => ({
          label: item.name, description: item.id === view.graph.root_agent?.id ? "Assistant root" : item.role_key ?? "unnamed role",
          detail: `Reports to: ${view.graph.agents.find(parent => parent.id === item.reports_to)?.name ?? "none"} · ${item.criteria ?? item.model?.id ?? "model unconfigured"} · ${item.reasoning}`, agent: item,
        })), { title: "Inspect or edit a server agent", matchOnDescription: true, matchOnDetail: true });
        if (agent) await this.editAgent(view, agent.agent);
      } else if (action.route === "workflows") {
        const value = await workspaceCommand(["workflows"]);
        if (!Array.isArray(value.workflows)) throw new Error("Invalid workflow catalog.");
        const rows = value.workflows.map(WorkflowExecution.selection).map(workflow => ({ label: workflow.name, description: workflow.revision, workflow }));
        if (!rows.length) { void vscode.window.showInformationMessage("No workflows in this server workspace."); return; }
        const workflow = await vscode.window.showQuickPick(rows, { title: "Server workflows", placeHolder: "Inspect, run a selected revision, or check execution status" });
        if (workflow) await this.workflow(view, workflow.workflow);
      } else await this.link(view, action.route);
    } catch (error) { void vscode.window.showErrorMessage(error instanceof Error ? error.message : "Workspace unavailable."); }
  }
  private async workflow(view: WorkspaceView, selected: WorkflowSelection): Promise<void> {
    const identity = `${view.workspace_id}/${selected.id}`;
    const execution = this.executions.get(identity) ?? new WorkflowExecution(selected);
    this.executions.set(identity, execution);
    for (;;) {
      const run = execution.run;
      const action = await vscode.window.showQuickPick([
        { label: "Input values", action: "inputs", description: execution.values },
        { label: execution.key ? "Retry the same pinned request" : "Run selected revision", action: "run", description: execution.workflow.revision },
        { label: "Check run status", action: "status", description: run ? `${run.status} · ${run.updatedAt}` : execution.key ? "response not confirmed" : "not submitted" },
        { label: "New execution", action: "new", description: "After completion or confirmed rejection" },
        { label: "Execution history", action: "history" },
        { label: "Open workflow editor", action: "open" },
      ], { title: `${selected.name} · ${run?.status ?? (execution.key ? "request retained" : "ready")}`,
        placeHolder: `${view.origin} · revision ${execution.workflow.revision}${execution.key ? ` · request ${execution.key}` : " · server checks execute permission"}` });
      if (!action) return;
      try {
        if (action.action === "inputs") {
          const values = await vscode.window.showInputBox({ title: "Workflow input values by name (JSON)", value: execution.values,
            prompt: "Object of input names and values; use {} when no inputs are required", ignoreFocusOut: true });
          if (values !== undefined) execution.values = values;
        } else if (action.action === "new") execution.reset(selected);
        else if (action.action === "open") await this.link(view, "workflows", selected.id);
        else if (action.action === "history") {
          const value = await workspaceCommand(["workflow-history", selected.id]);
          const document = await vscode.workspace.openTextDocument({ language: "json", content: JSON.stringify(value.runs, null, 2) });
          await vscode.window.showTextDocument(document, { preview: true, preserveFocus: true });
        } else {
          const result = await vscode.window.withProgress({ location: vscode.ProgressLocation.Window, title: action.action === "run" ? "Submit selected workflow revision" : "Read execution status" },
            () => action.action === "run" ? execution.submit() : execution.refresh());
          if (result) await vscode.window.showInformationMessage(`${selected.name}: ${result.status} · run ${result.id} · updated ${result.updatedAt}`);
          else await vscode.window.showInformationMessage(execution.settled ? "Request already resolved. Review inputs and choose New execution." : "Request not found yet. Retry with the same key; do not create another execution.");
        }
      } catch (error) { await vscode.window.showErrorMessage(error instanceof Error ? error.message : "Workflow request failed; request retained."); }
    }
  }
  private async link(view: WorkspaceView, route: string, identity?: string): Promise<void> {
    const value = await workspaceCommand(["link", route, ...(identity ? ["--identity", identity] : [])]);
    if (typeof value.url !== "string" || new URL(value.url).origin !== view.origin) throw new Error("Invalid workspace link.");
    await vscode.env.openExternal(vscode.Uri.parse(value.url));
  }
  private async editAgent(view: WorkspaceView, agent: WorkspaceAgent): Promise<void> {
    const draft = this.drafts.get(agent.id) ?? { revision: view.graph.revision, edit: {} };
    this.drafts.set(agent.id, draft);
    for (;;) {
      const action = await vscode.window.showQuickPick([
        { label: "Ranked models", key: "ranking", description: "Saved criteria · across desktop providers", detail: "Inspect the ordered candidates; availability is checked when the agent starts" },
        { label: "Configured server model", key: "model", description: (draft.edit.model !== undefined ? draft.edit.model : agent.model)?.id ?? "resolve from criteria" },
        { label: "Model criteria", key: "criteria", description: draft.edit.criteria ?? agent.criteria ?? "unset" },
        { label: "Criteria weights", key: "criteria_weights", description: draft.edit.criteria_weights ?? agent.criteria_weights },
        { label: "Reasoning", key: "reasoning", description: draft.edit.reasoning ?? agent.reasoning },
        { label: "Reporting parent", key: "reports_to", description: view.graph.agents.find(item => item.id === (draft.edit.reports_to !== undefined ? draft.edit.reports_to : agent.reports_to))?.name ?? "none" },
        { label: "Save agent changes", key: "save", description: view.writable ? `${Object.keys(draft.edit).length} changed fields` : "read-only connection" },
        { label: "Set as Assistant root", key: "root" },
        { label: "Open full graph editor", key: "open" },
        { label: "Open server prompt library", key: "prompts" },
        { label: "Reload and discard this draft", key: "reload" },
      ], { title: `${agent.name} · revision ${agent.revision}`, placeHolder: `${view.origin} · changes persist only with Save` });
      if (!action) return; // draft survives reopening this control
      if (action.key === "ranking") {
        const items = (async () => {
          const result = await interactCli(["agents", "policy", "--json-out"]);
          if (result.error) throw new Error(result.error);
          const value = JSON.parse(result.stdout);
          if (!Array.isArray(value.agents)) throw new Error("Model ranking unavailable. Retry with the matching Interact runtime.");
          const rule = (value.agents as PolicyRule[]).find(item => item.name === agent.role_key);
          if (!rule) throw new Error("This agent has no named desktop role in the saved policy.");
          if (!rule.ranked?.length) throw new Error(rule.why ?? "No ranked candidates for this saved model rule.");
          return rule.ranked.map((candidate, index) => ({
            label: `${index + 1}. ${candidate.model}`, description: candidate.provider,
            detail: `${rule.rule} · availability checked at launch`,
          }));
        })();
        try {
          await vscode.window.showQuickPick(items, { title: `Saved model ranking · ${agent.name}`,
            placeHolder: "Top candidates in criteria order; inspecting does not change the agent or its draft", matchOnDetail: true });
        } catch (error) { await vscode.window.showErrorMessage(error instanceof Error ? error.message : "Model ranking unavailable."); }
        continue;
      }
      if (action.key === "open") { await this.link(view, "agents", agent.id); continue; }
      if (action.key === "prompts") { await this.link(view, "prompts"); continue; }
      if (action.key === "reload") { this.drafts.delete(agent.id); await this.open(agent.id); return; }
      if (!view.writable) { void vscode.window.showInformationMessage("Connection is read-only. Open the signed-in graph editor to change server agents."); continue; }
      if (action.key === "save" || action.key === "root") {
        try {
          const args = action.key === "root" ? ["root-set", agent.id] : ["agent-edit", agent.id, "--edit-json", JSON.stringify(draft.edit)];
          const saved = parseWorkspace(await workspaceCommand([...args, "--expected-revision", draft.revision]));
          acceptWorkspace(saved);
          if (action.key === "save") this.drafts.delete(agent.id);
          else { draft.revision = saved.graph.revision; if ("reports_to" in draft.edit) draft.edit.reports_to = null; }
          this.changed();
          void vscode.window.showInformationMessage(action.key === "root" ? "Assistant root saved on server." : "Agent revision saved on server.");
          return;
        } catch (error) { await vscode.window.showErrorMessage(error instanceof Error ? error.message : "Save failed; draft retained."); continue; }
      }
      if (action.key === "model") {
        try {
          const value = await workspaceCommand(["models"]);
          if (!Array.isArray(value.models)) throw new Error("Invalid configured model list.");
          const models = value.models.map(parseWorkspaceModel);
          const model = await vscode.window.showQuickPick([{ label: "Resolve from criteria at launch", model: null as WorkspaceModel | null },
            ...models.map(model => ({ label: model.id, description: `connection ${model.connection.id}`, detail: `revision ${model.connection.revision}`, model }))],
            { title: "Choose an existing server provider connection and model" });
          if (model) draft.edit.model = model.model;
        } catch (error) { await vscode.window.showErrorMessage(error instanceof Error ? error.message : "Model list unavailable."); }
      } else if (action.key === "reports_to") {
        const parent = await vscode.window.showQuickPick([{ label: "No reporting parent", id: null as string | null }, ...view.graph.agents.filter(item => item.id !== agent.id).map(item => ({ label: item.name, id: item.id }))], { title: "Choose reporting parent; server rejects cycles" });
        if (parent) draft.edit.reports_to = parent.id;
      } else if (action.key === "reasoning") {
        const reasoning = await vscode.window.showQuickPick(["minimal", "low", "medium", "high", "xhigh", "max", "ultra"], { title: "Reasoning level" });
        if (reasoning) draft.edit.reasoning = reasoning;
      } else {
        const key = action.key as "criteria" | "criteria_weights";
        const text = await vscode.window.showInputBox({ title: action.label, value: draft.edit[key] ?? agent[key] ?? "", prompt: "Validated by the shared server record adapter when saved", ignoreFocusOut: true });
        if (text !== undefined) { if (key === "criteria") draft.edit.criteria = text.trim() || null; else draft.edit.criteria_weights = text; }
      }
    }
  }
}
