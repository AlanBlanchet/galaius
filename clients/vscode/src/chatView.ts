/** The Chat view — the side-bar surface you TALK to an agent from.
 *
 *  The tree lists what is running; the conversation tab shows what happened. Both are windows.
 *  This one is a conversation: it sits under the agent list in the same side-bar container, shows
 *  the selected agent's transcript, and has a composer that sends a message back. The agent
 *  resumes its own session, so it answers with everything it has already done still in context.
 *
 *  Sending shells out to `interact agents send` rather than reimplementing delivery: the CLI and
 *  the MCP tool share one set of refusals (an unknown run, one of your own editor sessions, a
 *  provider that cannot resume), and this must not drift from them.
 */
import * as fs from "fs";
import * as vscode from "vscode";

import { AgentRun as StoredAgentRun, activityOf, readAgentRuns } from "./agents";
import { chatFiles } from "./chatFiles";
import { CHAT_COMMANDS } from "./chatCommands";
import { agentLabel, conversationTitle, roleOf } from "./roster";
import { agentDocument, agentView } from "./agentPanel";
import { CompetenceStore } from "./competenceStore";
import { DIM_FOREGROUND } from "./themeTokens";
import { modelChosenFor } from "./agentModels";
import { companyOf, definitionFile, readOrg } from "./org";
import { teamSpend } from "./teamSpend";
import { scopeStore } from "./scopeStore";
import { interactCli } from "./interactCli";
import { describeMode, knownModes, type PermissionMode } from "./permissionModes";
import { runStatusOf, type RunStatus } from "./runStatus";
import { billingPresentation, type BillingPresentation } from "./billingPresentation";
import {
  CONVERSATION_ACTIVITY_EVENT_LIMIT,
  boundedConversationActivityChildren,
  chatDocument,
  conversationApprovalKey,
  conversationApprovalsAfterEvent,
  conversationActivityFragment,
  mergeConversationRunSnapshot,
  conversationStateAfterEvent,
  isAwaitingReply,
  transcriptFragment,
  validatedInteractionSubmission,
  type ConversationActivityView,
  type ConversationConsoleState,
} from "./conversationFormat";
import {
  createConversationClient,
  usesConversationTransport,
  type ConversationClient,
  type ConversationClientState,
} from "./conversationClient";
import { sessionTabs } from "./sessionTabs";
import { IO_SCHEME, ioTarget } from "./ioDocument";
import { agentsDir } from "./paths";
import { conversationExtensionVersion, resolveConversationBackend } from "./conversationBackend";
import type {
  AgentEvent,
  AgentRun,
  InteractionSubmission,
  ModelSelection,
} from "./generated/types";

type ConversationRun = Omit<AgentRun, "status"> & {
  status: Exclude<RunStatus, "declared">;
};

function currentConversationRuns(views: readonly ConversationActivityView[]): AgentRun[] {
  return views.flatMap((view) => [view.run, ...currentConversationRuns(view.children)]);
}

function conversationRun(run: AgentRun | StoredAgentRun): ConversationRun | undefined {
  const status = runStatusOf(run.status);
  if (status === "declared") return undefined;
  return { ...run, status };
}

export class ChatViewProvider implements vscode.WebviewViewProvider {
  private catalogSnapshot: import("./generated/types").ConversationCatalog | undefined;
  private readonly workspaceSubscription: vscode.Disposable;

  public conversationCatalog(): import("./generated/types").ConversationCatalog | undefined {
    return this.catalogSnapshot;
  }
  public static readonly viewId = "interactAgents.chat";

  /** Set while a conversation is open. The roster views' `when` clauses watch it, so the column
   *  belongs to whichever of the two you are actually using. */
  public static readonly IN_CONVERSATION = "interact.inConversation";

  /** Hand the column back to the roster. */
  public static leaveConversation(): void {
    void vscode.commands.executeCommand(ChatViewProvider.IN_CONVERSATION_CLEAR);
  }

  /** Repaint the agent depth if it is what you are looking at.
   *
   *  Choosing a model wrote the file and toasted, but the open panel kept showing the old one until
   *  you navigated away and back — which defeats the reason the override is rendered at all: a
   *  choice you cannot see is one you forget you made. */
  public repaintAgent(agent: string): void {
    if (this.agentId === agent) this.render();
  }

  /** Leave whatever depth you are at and return to the team.
   *
   *  The command used to only flip a context key and re-reveal the Team tab. That worked when the
   *  key HID the roster view — but the roster moved to the big panel and the key now gates
   *  nothing, so "back" became a button that did nothing at every depth, with no way out of a
   *  conversation except clicking a different agent. State has to be cleared and the panel
   *  repainted; a context key is not navigation.
   */
  public backToTeam(): void {
    this.agentId = null;
    this.runId = undefined;
    this.rendered = undefined;
    this.render();
  }

  /** Open the existing conversation composer, whose route catalog owns model and provider choice. */
  public async newConversation(): Promise<void> {
    this.backToTeam();
    await vscode.commands.executeCommand("setContext", ChatViewProvider.IN_CONVERSATION, true);
    if (this.view) void this.view.show?.(true);
    await vscode.commands.executeCommand("interactAgents.chat.focus");
    this.render();
  }

  private static readonly IN_CONVERSATION_CLEAR = "interact.agents.backToTeam";

  private view: vscode.WebviewView | undefined;
  /** What each model is measured at — the one store every surface shares. */
  private readonly measured = new CompetenceStore();
  private runId: string | undefined;
  private watcher: fs.FSWatcher | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private conversationProjection: ReturnType<typeof setTimeout> | undefined;
  private conversationClient: ConversationClient | undefined;
  private conversationGeneration = 0;
  private consoleState: ConversationConsoleState = { phase: "loading" };
  private readonly liveRuns = new Map<string, ConversationRun>();
  private readonly approvals = new Map<string, { run_id: string; event: AgentEvent }>();

  private readonly disposeOnProcessExit = (): void => this.dispose();

  constructor(private readonly log: vscode.OutputChannel) {
    process.once("exit", this.disposeOnProcessExit);
    this.workspaceSubscription = vscode.workspace.onDidChangeWorkspaceFolders(() => {
      this.catalogSnapshot = undefined;
      this.conversationGeneration += 1;
      this.conversationClient?.dispose();
      this.conversationClient = undefined;
    });
  }

  /** The host belongs to the extension window, not to the visibility lifetime of one webview. */
  public dispose(): void {
    process.off("exit", this.disposeOnProcessExit);
    this.stopWatching();
    this.workspaceSubscription.dispose();
    this.conversationGeneration += 1;
    this.catalogSnapshot = undefined;
    this.conversationClient?.dispose();
    this.conversationClient = undefined;
  }

  /** Point the panel at an agent — what clicking a row in the list, or a body in the workplace,
   *  does.
   *
   *  Reveals the CONTAINER first when the view has never resolved. `view?.show()` is a no-op
   *  until VS Code has instantiated the webview, so clicking somebody in the team floor with the
   *  panel closed did nothing at all, silently — the worst kind of dead control, because it looks
   *  like the click missed.
   */
  public show(runId: string): void {
    this.agentId = null;  // opening a conversation leaves the agent depth
    void this.reveal(runId);
  }

  /** The middle depth: an agent, its identity, and the tasks it was given.
   *
   *  "on the sidepanel, we should be able to view what TASKS an agent was given, and then proceed
   *  to view the conversation we want" — this is that view, and the place the two things you
   *  MANAGE about an agent live: its definition file and the model it runs on.
   */
  public showAgent(agent: string): void {
    this.agentId = agent;
    this.runId = undefined;
    this.rendered = undefined;
    void this.revealAgent();
  }

  private async revealAgent(): Promise<void> {
    this.render();
    await vscode.commands.executeCommand("setContext", ChatViewProvider.IN_CONVERSATION, true);
    if (this.view) { void this.view.show?.(true); }
    await vscode.commands.executeCommand("interactAgents.chat.focus");
    this.render();
  }

  /** Awaited, because the Chat view now carries `when: interact.inConversation` — it does not
   *  exist to focus until that key is actually set, and `setContext` is asynchronous. Firing both
   *  and hoping is how a click lands on nothing. */
  private async reveal(runId: string): Promise<void> {
    this.runId = runId;
    this.render();
    // "The chat page shouldn't be small... i'm having to close the menus for team and agent."
    // The roster views' `when` clauses watch this key, so opening a conversation gives it the whole
    // column with nobody collapsing anything by hand.
    //
    // FIRST, and unconditionally. This lived inside the `!this.view` branch below and was therefore
    // dead in every normal open: the Chat webview has no `when` of its own, so it resolves as soon
    // as the container is visible and `this.view` is already truthy the first time anyone clicks a
    // row. A visual-critic round reproduced it live in both themes — the Team list simply never
    // stepped aside.
    await vscode.commands.executeCommand("setContext", ChatViewProvider.IN_CONVERSATION, true);
    if (this.view) {
      // Reveal only on an explicit pick, so following a live run never steals the side bar.
      void this.view.show?.(true);
      return;
    }
    await vscode.commands.executeCommand("interactAgents.chat.focus");
  }

  public resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    // This view now carries `when: interact.inConversation`, so VS Code DISPOSES it every time the
    // clause goes false — and a disposed webview whose reference we kept throws on the next write.
    // That made the SECOND conversation you opened blank the whole sidebar with no visible way
    // back: `Error: Webview is disposed` out of render(), sidebar chrome and nothing in it.
    // Forget it here and the next show() resolves a fresh one. The roster view always guarded this;
    // this file never needed to until a `when` clause made it disposable too.
    // Paint when it actually becomes visible. render() now refuses to write to a hidden view, and
    // a `when`-gated view can resolve a moment before VS Code shows it — without this the first
    // conversation of a session could resolve to an empty panel.
    view.onDidChangeVisibility(() => { if (view.visible) this.render(); });
    view.onDidDispose(() => {
      if (this.view === view) this.view = undefined;
      this.rendered = undefined;  // the next view starts empty; a stale id would skip the paint
    });
    // Asked once, not per render: the answer only changes when the CLI is upgraded, and a panel
    // that shells out on every repaint is a panel that stutters while an agent is working. The
    // memoisation lives in knownModes(), shared with the spawn picker and the workspace default —
    // this was three shell-outs for one answer, with three different error handlings.
    void knownModes().then((modes) => { this.modes = modes; });
    // `enableCommandUris` is what lets a file link actually open the file: without it VS Code
    // silently drops the command: href, which looks exactly like a dead button.
    view.webview.options = { enableScripts: true, enableCommandUris: true };
    view.webview.onDidReceiveMessage((msg) => {
      if (msg?.type === "ready") this.replayConversationView();
      if (msg?.type === "reload-conversation") {
        this.conversationClient?.dispose();
        this.conversationClient = undefined;
        this.consoleState = { phase: "loading" };
        this.rendered = undefined;
        this.render();
        void this.loadConversationConsole();
      }
      if (msg?.type === "send" && typeof msg.text === "string") void this.send(msg.text);
      if (msg?.type === "start" && typeof msg.text === "string"
          && typeof msg.routeId === "string"
          && (msg.selectionKind === "model" || msg.selectionKind === "criterion")
          && typeof msg.selection === "string") {
        void this.start(msg.text, msg.routeId, msg.selectionKind, msg.selection);
      }
      if (msg?.type === "cancel" && typeof msg.runId === "string") void this.cancel(msg.runId);
      if (msg?.type === "interaction" && typeof msg.runId === "string"
          && msg.submission !== null && typeof msg.submission === "object"
          && typeof msg.submission.interaction_id === "string") {
        const key = conversationApprovalKey(msg.runId, msg.submission.interaction_id);
        const waiting = this.approvals.get(key);
        const interaction = waiting?.event.interaction;
        const submission = interaction
          ? validatedInteractionSubmission(interaction, msg.submission)
          : undefined;
        if (waiting?.run_id === msg.runId && submission) {
          void this.submitInteraction(msg.runId, submission);
        }
      }
      if (msg?.type === "back") { this.agentId = null; void ChatViewProvider.leaveConversation(); }
      // The empty state's door: an empty panel must lead somewhere, not describe a missing list.
      if (msg?.type === "openTeam") void vscode.commands.executeCommand("interact.agents.team");
      // The two things you MANAGE about an agent, from the depth where the agent IS the subject.
      if (msg?.type === "agentAction" && typeof msg.agent === "string") {
        if (msg.action === "model") void vscode.commands.executeCommand("interact.agents.model", msg.agent);
        if (msg.action === "definition" && typeof msg.path === "string") {
          void vscode.window.showTextDocument(vscode.Uri.file(msg.path));
        }
      }
      // Walking UP the tree: the errand that produced this answer.
      if (msg?.type === "openRun" && typeof msg.runId === "string") this.show(msg.runId);
      // The whole input/output of one tool call, in its own read-only tab — the Claude Code
      // gesture. A stamped call is served from the raw stream by the vendor's tool id; an
      // unstamped one (records predating the stamping) serves the stored text the webview
      // handed over. The id and text are opaque data — a hostile id can only fail to match.
      if (msg?.type === "io" && typeof msg.toolId === "string"
          && (msg.side === "in" || msg.side === "out") && this.runId) {
        const path = ioTarget(this.runId, msg.toolId, msg.side,
          String(msg.tool ?? "tool"), typeof msg.text === "string" ? msg.text : "");
        if (path) {
          const uri = vscode.Uri.from({ scheme: IO_SCHEME, path });
          void vscode.window.showTextDocument(uri, { preview: true });
        }
      }
      if (msg?.type === "open" && typeof msg.path === "string") void this.open(msg.path);
      // A command from the slash menu. Checked against the declared list rather than executed as
      // given: the webview renders agent output, so anything arriving from it is untrusted, and
      // running an arbitrary command id because a message said so would be a real hole.
      if (msg?.type === "pickFile") void this.mentionFile();
      if (msg?.type === "command" && typeof msg.command === "string") {
        const known = CHAT_COMMANDS.find((c) => c.command === msg.command);
        if (!known) return;
        const run = this.run();
        if (known.needsAgent && !run) {
          void vscode.window.showInformationMessage(`${known.slash} needs an agent open.`);
          return;
        }
        void vscode.commands.executeCommand(known.command, run ? { run } : undefined);
      }
    });
    view.onDidDispose(() => this.stopWatching());
    this.watch();
    this.render();
    void this.loadConversationConsole();
  }

  private run(): StoredAgentRun | undefined {
    // The registry first, then the scope store's merged view — which is where your OWN
    // discovered sessions live. Without the fallback a "your session" row opened onto the
    // empty hint: the id was real, the lookup just never asked the list that holds it.
    return readAgentRuns().find((r) => r.run_id === this.runId)
      ?? scopeStore()?.runs().find((r) => r.run_id === this.runId)
      ?? this.liveRuns.get(this.runId ?? "");
  }

  private canContinue(run: Pick<StoredAgentRun, "kind" | "capabilities">): boolean {
    return !usesConversationTransport(run) || Boolean(run.capabilities?.includes("resume"));
  }

  private console(): ConversationConsoleState {
    return { ...this.consoleState, approvals: [...this.approvals.values()] };
  }

  /** Only an existing local workspace is permitted to scope provider processes and prompts. */
  private workspaceRoot(): string {
    const folder = vscode.workspace.workspaceFolders?.find(({ uri }) => uri.scheme === "file");
    if (!folder) throw new Error("Open a local workspace before starting a conversation.");
    try {
      const root = fs.realpathSync(folder.uri.fsPath);
      if (!fs.statSync(root).isDirectory()) throw new Error("not a directory");
      return root;
    } catch {
      throw new Error("The local workspace is unavailable.");
    }
  }

  /** Start through the conversation backend: an explicit dev checkout or a same-version local
   * `interact` executable from PATH. It does not consult the general server resolver or network. */
  private async loadConversationConsole(): Promise<void> {
    if (this.conversationClient) return;
    const generation = ++this.conversationGeneration;
    let client: ConversationClient | undefined;
    try {
      const workspaceRoot = this.workspaceRoot();
      const backend = await resolveConversationBackend({
        projectPath: vscode.workspace.getConfiguration("interact").get<string>("projectPath")
          || process.env.INTERACT_PROJECT_PATH,
        extensionVersion: conversationExtensionVersion(),
        workspaceRoot,
      });
      if (!backend.available) throw new Error(backend.reason);
      client = createConversationClient({
        command: backend.command,
        args: backend.args,
        cwd: workspaceRoot,
        origin: backend.origin,
      }, {
        // The bridge's stderr tail. It never reaches the UI (untrusted child output), but it must
        // reach SOMETHING — computed, bounded and then dropped is not a diagnostic, and this text
        // is what identified a real version skew.
        onDiagnostic: (line) => this.log.appendLine(`[bridge] ${line}`),
        onEvent: (run, event) => {
          if (this.conversationGeneration === generation && this.conversationClient === client) {
            this.onConversationEvent(run, event);
          }
        },
        onError: (message) => {
          if (this.conversationGeneration === generation && this.conversationClient === client) {
            this.conversationBridgeFailed(message);
          }
        },
        onState: (state) => {
          if (this.conversationGeneration === generation && this.conversationClient === client) {
            this.conversationClientStateChanged(state);
          }
        },
      });
      this.conversationClient = client;
      const catalog = await client.catalog();
      if (this.conversationGeneration !== generation || this.conversationClient !== client) return;
      this.catalogSnapshot = catalog;
      this.consoleState = {
        phase: catalog.routes.some((route) => route.availability === "available") ? "ready" : "empty",
        catalog,
      };
      this.rendered = undefined;
      this.render();
    } catch (error) {
      if (this.conversationGeneration !== generation
          || (client !== undefined && this.conversationClient !== client)) return;
      this.conversationBridgeFailed(
        error instanceof Error ? error.message : "The local conversation bridge could not start.",
      );
    }
  }

  private conversationClientStateChanged(state: ConversationClientState): void {
    if (state !== "connecting" || this.consoleState.catalog) return;
    this.consoleState = { phase: "loading" };
  }

  private conversationBridgeFailed(message: string): void {
    const visible = message || "The local conversation bridge stopped unexpectedly.";
    const repaintColdDocument = !this.consoleState.catalog;
    this.catalogSnapshot = undefined;
    this.consoleState = {
      ...this.consoleState,
      phase: "error",
      active_run_id: undefined,
      error: visible,
    };
    this.postConsoleState(visible, true, false);
    this.postConversationActivity();
    if (repaintColdDocument) {
      this.rendered = undefined;
      this.render();
    }
  }

  private postConsoleState(message: string, error: boolean, canSend: boolean): void {
    const run = this.run();
    void this.view?.webview.postMessage({
      type: "console-state",
      message,
      error,
      canSend: canSend && (!run || this.canContinue(run)),
      activeRunId: this.consoleState.active_run_id ?? null,
    });
  }

  /** A newly assigned webview document cannot receive messages until its script has installed
   * listeners. Replay state on its explicit readiness handshake so a first-and-only approval or
   * terminal event is not lost between `webview.html = ...` and script startup. */
  private replayConversationView(): void {
    const run = this.run();
    if (!run || usesConversationTransport(run)) {
      const phase = this.consoleState.phase;
      const message = this.consoleState.error
        ?? (phase === "starting"
          ? "Starting the conversation…"
          : phase === "pending" ? "Waiting for the current turn" : "");
      this.postConsoleState(message, Boolean(this.consoleState.error), phase === "ready");
    }
    const displayed = run ? conversationRun(run) : undefined;
    if (displayed) this.postRunStatus(displayed);
    this.postConversationActivity();
  }

  /** Status is independently patchable: provider activity must not unlock the composer or remove
   * its cancel button merely because the run changed from running to waiting (or back again). */
  private postRunStatus(run: Pick<AgentRun, "run_id" | "status">): void {
    if (run.run_id !== this.runId) return;
    void this.view?.webview.postMessage({ type: "console-state", runStatus: run.status });
  }

  private rememberConversationRun(run: ConversationRun): ConversationRun {
    const merged = conversationRun(mergeConversationRunSnapshot(this.liveRuns.get(run.run_id), run));
    if (!merged) return run;
    this.liveRuns.set(merged.run_id, merged);
    return merged;
  }

  private onConversationEvent(run: AgentRun, event: AgentEvent): void {
    const incoming = conversationRun(run);
    if (!incoming) return;
    const displayed = this.rememberConversationRun(incoming);
    this.postRunStatus(displayed);
    if (event.kind === "interaction" && event.interaction) {
      this.approvals.set(
        conversationApprovalKey(displayed.run_id, event.interaction.id),
        { run_id: displayed.run_id, event },
      );
    }
    const retainedApprovals = conversationApprovalsAfterEvent(
      [...this.approvals.values()], displayed, event,
    );
    if (retainedApprovals.length !== this.approvals.size) {
      this.approvals.clear();
      for (const approval of retainedApprovals) {
        const approvalId = approval.event.interaction?.id;
        if (approvalId) {
          this.approvals.set(
            conversationApprovalKey(approval.run_id, approvalId),
            approval,
          );
        }
      }
    }
    const nextState = conversationStateAfterEvent(this.consoleState, displayed, event);
    if (nextState !== this.consoleState) {
      this.consoleState = nextState;
      this.postConsoleState(nextState.error ?? "", Boolean(nextState.error), true);
    }
    this.scheduleConversationProjection();
  }

  private scheduleConversationProjection(): void {
    if (this.conversationProjection !== undefined) return;
    this.conversationProjection = setTimeout(() => {
      this.conversationProjection = undefined;
      this.postConversationActivity();
      this.render();
    }, 0);
  }

  private conversationRuns(): ConversationRun[] {
    const runs = new Map<string, ConversationRun>();
    for (const run of readAgentRuns()) {
      const displayed = conversationRun(run);
      if (displayed) runs.set(displayed.run_id, displayed);
    }
    for (const run of this.liveRuns.values()) {
      const merged = conversationRun(mergeConversationRunSnapshot(runs.get(run.run_id), run));
      if (merged) runs.set(run.run_id, merged);
    }
    return [...runs.values()];
  }

  private conversationActivity(): {
    activity: ConversationActivityView[];
    billing: BillingPresentation;
    spend: ReturnType<typeof teamSpend>;
  } {
    const selected = this.runId ? this.conversationRuns().find((run) => run.run_id === this.runId) : undefined;
    if (!selected) return {
      activity: [], billing: billingPresentation([]), spend: teamSpend([], undefined),
    };
    const byId = new Map(this.conversationRuns().map((run) => [run.run_id, run]));
    let root = selected;
    const ancestry = new Set<string>([root.run_id]);
    while (root.parent_run_id && byId.has(root.parent_run_id) && !ancestry.has(root.parent_run_id)) {
      ancestry.add(root.parent_run_id);
      root = byId.get(root.parent_run_id)!;
    }
    const rootId = selected.root_run_id ?? root.run_id;
    const family = this.conversationRuns()
      .filter((run) => (run.root_run_id ?? run.run_id) === rootId || run.run_id === rootId)
      .sort((left, right) => (left.started_at ?? 0) - (right.started_at ?? 0));
    const children = new Map<string, ConversationRun[]>();
    for (const run of family) {
      if (!run.parent_run_id) continue;
      children.set(run.parent_run_id, [...(children.get(run.parent_run_id) ?? []), run]);
    }
    const project = (run: ConversationRun, seen: ReadonlySet<string>): ConversationActivityView => {
      const nextSeen = new Set(seen).add(run.run_id);
      const turns = activityOf(run, CONVERSATION_ACTIVITY_EVENT_LIMIT);
      const currentTool = [...turns].reverse().find((turn) => turn.kind === "tool")?.tool ?? null;
      const boundedChildren = boundedConversationActivityChildren(children.get(run.run_id) ?? []);
      return {
        run,
        current_tool: currentTool,
        transcript: run.run_id === selected.run_id ? [] : turns,
        children: boundedChildren.items
          .filter((child) => !nextSeen.has(child.run_id))
          .map((child) => project(child, nextSeen)),
        omitted_children: boundedChildren.omitted,
      };
    };
    const rootRun = family.find((run) => run.run_id === rootId) ?? root;
    return {
      activity: [project(rootRun, new Set())],
      billing: billingPresentation(family.map((run) => ({
        chargePath: run.charge_path ?? "unknown",
        costCertainty: run.cost_certainty ?? "unknown",
        costUsd: run.cost_usd ?? null,
      }))),
      spend: teamSpend(family, selected.run_id),
    };
  }

  private postConversationActivity(): void {
    if (!this.view || !this.runId) return;
    const projection = this.conversationActivity();
    void this.view.webview.postMessage({
      type: "activity",
      html: conversationActivityFragment(
        projection.activity, this.console(), projection.billing, projection.spend,
      ),
    });
  }

  /** Which run the live document was built for. A different agent needs a new document; the SAME
   *  agent going on working needs only its transcript swapped. */
  /** undefined means no document; null means the cold composer document is already live. */
  private rendered: string | null | undefined;
  /** When set, the panel is at the AGENT depth: who this is, and the tasks it was given. The
   *  conversation is one level deeper. */
  private agentId: string | null = null;
  /** The autonomy levels this machine's CLI offers, read once — turning an id recorded on a run
   *  ("acceptEdits") into the words somebody chose it by ("May edit files"). */
  private modes: PermissionMode[] = [];

  /** Asked once for every surface that shows a number; the board on disk fills what the CLI
   *  could not. */
  private ensureCompetence(models: readonly string[]): void {
    void this.measured.ensure(models).then((known: boolean) => { if (known) this.render(); });
  }

  /** The agent depth. Identity from the company file, tasks from the registry. */
  private renderAgent(agent: string): void {
    if (!this.view) return;
    const org = readOrg();
    const declared = org?.agents.find((a) => a.name === agent);
    // Resolved identity, NOT the literal field. A definition-less run's `agent` is null and its id
    // ("main", the coordinator) exists only through roleOf's fallback — so filtering on the raw
    // field showed "No tasks yet" for the coordinator while five of its errands were visibly
    // failing in the room. That was precisely the case this whole change was written for.
    const company = companyOf(readOrg()) ?? undefined;
    const tasks = readAgentRuns().filter((r) => roleOf(r as never, company).id === agent);
    // Asked BY NAME: a run names a model the ranking may not carry verbatim, and only a named ask
    // resolves it. Never awaited by a paint — the panel draws at once and repaints when it lands.
    this.ensureCompetence(tasks.map((r) => r.model).filter((m): m is string => !!m));
    const ranModel = [...tasks]
      .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
      .find((t) => t.model)?.model ?? null;
    const body = agentView(
      {
        id: agent,
        title: declared?.title ?? null,
        department: declared?.department ?? null,
        model: modelChosenFor(agent),
        declaredModel: declared?.model ?? null,
        // Most agents declare `inherit`, so the declaration alone says nothing about them. What
        // they LAST ran on does, and it has a measure.
        ranModel,
        competence: (() => {
          const m = modelChosenFor(agent) ?? (declared?.model && declared.model !== "inherit"
            ? declared.model : ranModel);
          return this.measured.competence(m ?? undefined) ?? null;
        })(),
        // Resolved, not the raw relative string the company file records — an unresolved path
        // opens nothing and the chip would look live while doing nothing.
        definitionPath: definitionFile(agent, org)
          ?? tasks.find((t) => t.definition_path)?.definition_path ?? null,
      },
      // Each errand carries its OWN model and measure: the panel used to assert one score for
      // every run an agent had made, contradicting the row that was clicked whenever they differed.
      tasks.map((t) => ({
        ...t,
        model: t.model ?? null,
        competence: this.measured.competence(t.model ?? undefined) ?? null,
      })) as never[],
      "N",
    );
    const nonce = Math.random().toString(36).slice(2) + Date.now().toString(36);
    this.view.webview.html = agentDocument(nonce, body, DIM_FOREGROUND);
    this.rendered = undefined;  // the next conversation must repaint, not be skipped as unchanged
  }

  private render(): void {
    // `visible` as well as present: a view can be resolved and hidden, and writing to one VS Code
    // has already torn down throws rather than no-ops.
    if (!this.view?.visible) return;
    if (this.agentId) { this.renderAgent(this.agentId); return; }
    const current = this.run();
    if (!current && this.rendered === null) return;
    if (current && this.rendered === current.run_id) {
      // Same agent, more to say: patch the transcript and leave the rest of the view alone.
      const turns = activityOf(current, 300);
      void this.view.webview.postMessage({
        type: "transcript",
        html: transcriptFragment({
          turns,
          name: current.name,
          awaitingReply: isAwaitingReply(turns),
        }),
      });
      this.postConversationActivity();
      return;
    }
    this.rendered = current?.run_id ?? null;
    const run = this.run();
    const turns = run ? activityOf(run, 300) : [];
    // Who sent this one on its errand. Resolved here (the renderer stays import-free) and named by
    // what the caller is DOING, so "↑ parent" reads as a place rather than an id.
    const all = readAgentRuns();
    const mine = all.find((r) => r.run_id === this.runId);
    const parentRun = mine?.parent_run_id ? all.find((r) => r.run_id === mine.parent_run_id) : undefined;
    const projection = this.conversationActivity();
    this.view.webview.html = chatDocument({
      parent: parentRun ? { runId: parentRun.run_id, title: conversationTitle(parentRun as never) } : null,
      // A fresh nonce per render: the CSP admits only scripts carrying it, so nothing that
      // arrives in an agent's output can execute even if the escaping were ever wrong.
      nonce: Math.random().toString(36).slice(2) + Date.now().toString(36),
      // The slash menu's entries. Omitted here for a whole release: the "/" button rendered, was
      // enabled, and opened an empty list in every state, because this one argument was optional
      // and never passed. It is required now, so the compiler refuses the omission.
      commands: CHAT_COMMANDS,
      turns,
      name: run?.name,
      status: run?.status,
      // One of your own sessions: shown in full, steered in its own window — the composer
      // says so instead of offering a Send the CLI would refuse.
      readOnly: run?.status === "foreign" || run?.kind === "provider_child",
      // Everyone on this errand: the entry agent first (the way back), then whoever it put to
      // work, then their own helpers. Live dots say who is still going while you read someone
      // else's transcript.
      tabs: run
        ? sessionTabs(all as never[], run.run_id,
            (r) => agentLabel(r as never, companyOf(readOrg()) ?? undefined))
        : [],
      awaitingReply: isAwaitingReply(turns),
      run: run
        ? ({ ...run, permission: describeMode(run.permission_mode, this.modes) } as never)
        : (run as never),
      // What the whole TEAM is costing, not only the agent being read. Imported and then never
      // called for a whole commit: the row rendered in the preview fixture, which passes its own
      // spend, so every screenshot of it was real and meant nothing about the panel.
      spend: teamSpend(
        scopeStore()?.runs() ?? readAgentRuns(),
        run?.run_id,
        currentConversationRuns(projection.activity),
      ),
      files: run ? chatFiles(run, agentsDir(), fs.existsSync) : [],
      sentBy: run?.parent_run_id
        ? readAgentRuns().find((r) => r.run_id === run.parent_run_id)?.name ?? null
        : null,
      console: this.console(),
      activity: projection.activity,
      billing: projection.billing,
    });
  }

  /** Offer a workspace file and hand its path back to the composer.
   *
   *  The webview cannot enumerate the workspace, so "@" comes here. Paths are relative to the
   *  folder, because that is what an agent working in that folder can actually open — an absolute
   *  path from this machine is noise in a brief.
   */
  private async mentionFile(): Promise<void> {
    const found = await vscode.workspace.findFiles("**/*", "**/{node_modules,.git,out,dist}/**", 2000);
    if (found.length === 0) {
      void vscode.window.showInformationMessage("No files in this workspace to mention.");
      return;
    }
    const picked = await vscode.window.showQuickPick(
      found.map((uri) => ({ label: vscode.workspace.asRelativePath(uri), uri })),
      { title: "Mention a file", matchOnDescription: true },
    );
    if (picked) this.view?.webview.postMessage({ type: "insert", text: `@${picked.label} ` });
  }

  /** Open a file in an editor — a webview cannot, so it asks us to.
   *
   *  A failure is SHOWN, not just logged: a button that silently does nothing is the defect, and
   *  the log is somewhere nobody looks until they already suspect one.
   */
  private async open(target: string): Promise<void> {
    try {
      const uri = vscode.Uri.file(target);
      // "when we click on it it opens the code DIFF in the file." For a source file with pending
      // changes, the git extension's own view IS that diff — the same one its gutter opens. It
      // no-ops or throws for an unchanged or untracked file, so the plain editor is the fallback,
      // and an image never goes near it (VS Code renders it in its own viewer).
      if (!/\.(png|jpe?g|gif|webp)$/i.test(target)) {
        try {
          await vscode.commands.executeCommand("git.openChange", uri);
          return;
        } catch {
          /* not in a repo, not modified, or no git extension — the file itself is still right */
        }
      }
      await vscode.commands.executeCommand("vscode.open", uri, { preview: true });
    } catch (err) {
      this.log.appendLine(`could not open ${target}: ${err}`);
      void vscode.window.showErrorMessage(`Interact: could not open ${target} — ${err}`);
    }
  }

  private async start(
    text: string,
    routeId: string,
    selectionKind: "model" | "criterion",
    selected: string,
  ): Promise<void> {
    const catalog = this.consoleState.catalog;
    const route = catalog?.routes.find((candidate) => candidate.id === routeId);
    if (!this.conversationClient || !catalog || route?.availability !== "available") {
      void this.view?.webview.postMessage({ type: "started", ok: false });
      return;
    }
    const value = selected.trim();
    const selection: ModelSelection = value
      ? selectionKind === "model" ? { model: value } : { criterion: value }
      : {};
    this.consoleState = {
      ...this.consoleState,
      phase: "starting",
      error: undefined,
    };
    this.postConsoleState("Starting the conversation…", false, false);
    try {
      const workspaceRoot = this.workspaceRoot();
      const started = await this.conversationClient.start({
        route_id: routeId,
        prompt: text,
        selection,
        workspace_root: workspaceRoot,
      });
      const incoming = conversationRun(started);
      if (!incoming) throw new Error("The conversation host returned an invalid run status.");
      const displayed = this.rememberConversationRun(incoming);
      this.runId = started.run_id;
      this.consoleState = {
        ...this.consoleState,
        phase: "pending",
        active_run_id: displayed.run_id,
        error: undefined,
      };
      void this.view?.webview.postMessage({ type: "started", ok: true });
      await vscode.commands.executeCommand("setContext", ChatViewProvider.IN_CONVERSATION, true);
      this.rendered = undefined;
      this.render();
    } catch {
      const crashed = this.conversationClient.state() === "crashed";
      const message = crashed
        ? "The local conversation bridge stopped unexpectedly. The turn was not retried."
        : "The selected route could not start this conversation. Nothing else was tried.";
      this.consoleState = {
        ...this.consoleState,
        phase: crashed ? "error" : "ready",
        error: message,
      };
      this.log.appendLine(`conversation start failed (${crashed ? "bridge stopped" : "route rejected"})`);
      void this.view?.webview.postMessage({ type: "started", ok: false });
      this.postConsoleState(message, true, !crashed);
    }
  }

  private async send(text: string): Promise<void> {
    const run = this.run();
    if (!run) {
      void this.view?.webview.postMessage({ type: "sent", ok: false });
      return;
    }
    if (!this.canContinue(run)) {
      void this.view?.webview.postMessage({ type: "sent", ok: false });
      return;
    }
    if (!usesConversationTransport(run)) {
      const { error, stdout } = await interactCli(["agents", "send", run.run_id, text]);
      const failed = Boolean(error) || stdout.trim().startsWith("ERROR");
      void this.view?.webview.postMessage({ type: "sent", ok: !failed });
      if (failed) {
        this.log.appendLine("process continuation failed (CLI rejected the request)");
        void vscode.window.showErrorMessage(`Could not reach ${run.name}. Your message was restored.`);
      } else {
        this.render();
      }
      return;
    }
    if (!this.conversationClient) {
      void this.view?.webview.postMessage({ type: "sent", ok: false });
      return;
    }
    this.consoleState = {
      ...this.consoleState,
      phase: "pending",
      active_run_id: run.run_id,
      error: undefined,
    };
    this.postConsoleState(`${run.name} is answering…`, false, false);
    try {
      const resumed = await this.conversationClient.send(run.run_id, text);
      const incoming = conversationRun(resumed);
      if (!incoming) throw new Error("The conversation host returned an invalid run status.");
      const displayed = this.rememberConversationRun(incoming);
      this.postRunStatus(displayed);
      void this.view?.webview.postMessage({ type: "sent", ok: true });
      this.render();
    } catch {
      const crashed = this.conversationClient.state() === "crashed";
      const message = crashed
        ? "The local conversation bridge stopped unexpectedly. The message was not retried."
        : `Could not reach ${run.name}. No fallback route was used.`;
      this.consoleState = {
        ...this.consoleState,
        phase: crashed ? "error" : "ready",
        active_run_id: undefined,
        error: message,
      };
      this.log.appendLine(`conversation continuation failed (${crashed ? "bridge stopped" : "route rejected"})`);
      void this.view?.webview.postMessage({ type: "sent", ok: false });
      this.postConsoleState(message, true, !crashed);
      this.postConversationActivity();
    }
  }

  private async cancel(runId: string): Promise<void> {
    if (!this.conversationClient || runId !== this.consoleState.active_run_id) return;
    try {
      const cancelled = await this.conversationClient.cancel(runId);
      const incoming = conversationRun(cancelled);
      if (!incoming) throw new Error("The conversation host returned an invalid run status.");
      const displayed = this.rememberConversationRun(incoming);
      this.postRunStatus(displayed);
      this.consoleState = { ...this.consoleState, phase: "ready", active_run_id: undefined };
      this.postConsoleState("", false, true);
      this.postConversationActivity();
      this.render();
    } catch {
      this.conversationBridgeFailed("The local conversation bridge could not confirm cancellation.");
    }
  }

  private async submitInteraction(
    runId: string,
    submission: InteractionSubmission,
  ): Promise<void> {
    const key = conversationApprovalKey(runId, submission.interaction_id);
    if (!this.conversationClient || !this.approvals.has(key)) return;
    try {
      const run = await this.conversationClient.interact(runId, submission);
      const incoming = conversationRun(run);
      if (!incoming) throw new Error("The conversation host returned an invalid run status.");
      const displayed = this.rememberConversationRun(incoming);
      this.postRunStatus(displayed);
      this.approvals.delete(key);
      this.postConversationActivity();
    } catch {
      this.conversationBridgeFailed("The local conversation bridge could not forward that decision.");
    }
  }

  /** Follow the registry so an agent opened mid-flight keeps updating as it works. */
  private watch(): void {
    this.stopWatching();
    try {
      this.watcher = fs.watch(agentsDir(), () => {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this.render(), 250); // coalesce a burst of writes
      });
    } catch (err) {
      this.log.appendLine(`chat view is not following changes: ${err}`);
    }
  }

  private stopWatching(): void {
    clearTimeout(this.timer);
    this.watcher?.close();
    this.watcher = undefined;
  }
}
