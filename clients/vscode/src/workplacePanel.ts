/** The TEAM view — the workplace you watch your agents work in.
 *
 *  An editor tab, not a side-bar section: a room full of people needs width, and the point is to
 *  leave it open on a second monitor and glance at it. The side bar keeps the list and the chat.
 *
 *  This file is the HOST only — it assembles a TeamState from the registry and hands it to the
 *  renderer. The drawing lives in webview/workplace/, so the visual can be reworked without
 *  touching the data, and the data can be fixed without touching the visual.
 */
import * as fs from "fs";
import * as vscode from "vscode";

import { readAgentActivity, readAgentMessages, readAgentRuns } from "./agents";
import { agentsDir } from "./paths";
import { buildTeam } from "./teamState";
import { selectedRunId } from "./workplaceMessage";
import { renderScene, renderWorkplace } from "./workplaceView";
import type { TeamState } from "./team";
import { scopeStore } from "./scopeStore";
import { facultiesOf, parseCapabilities } from "./capabilities";
import { companyOf, readOrg } from "./org";
import { claimColumn, nextColumn, releaseColumn } from "./panelColumn";
import { buildRail, railRoute } from "./rail";
import { ROSTER_VIEWS, modelsOnScreen, railBody, railScript, railStyle,
  type RosterView } from "./railHtml";
import { shortCompetence } from "./competence";
import { CompetenceStore } from "./competenceStore";
import { actionsFor } from "./agentActions";
import { agentLabel, conversationTitle, roleOf } from "./roster";
import { voiceOf } from "./statusLanguage";
import { lastObservedAt } from "./teamState";
import { describeScope, projectFor } from "./workspaceScope";

export class WorkplacePanel {
  private static current: WorkplacePanel | undefined;
  /** What each model is measured at — the one store every surface shares. */
  private readonly measured = new CompetenceStore();
  private watcher: fs.FSWatcher | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    private readonly log: vscode.OutputChannel,
    /** Where a UI preference is remembered between windows. */
    private readonly store?: vscode.Memento,
  ) {
    this.panel.onDidDispose(() => this.dispose());
    // One column for galaius's surfaces: a new one joins the group its siblings already
    // hold rather than opening yet another beside your code.
    claimColumn("workplace", this.panel.viewColumn);
    // A click in the room aims the side-bar Chat at that agent: the workplace is where you SEE
    // the team, the chat is where you talk to one of them, and this is the seam between.
    this.panel.webview.onDidReceiveMessage((msg) => {
      // The roster's view chooser. Handled before anything else because it is the one message
      // that is a PREFERENCE rather than a navigation: it changes how the list looks and nothing
      // about where you are in it.
      const view = (msg as { type?: string; view?: string } | null)?.type === "rosterView"
        ? (msg as { view?: string }).view : undefined;
      if (view) { void this.chooseView(view as RosterView); return; }
      // Two shapes on purpose: select/run_id is what the pixel-art scene posts, focus/runId what
      // the plain fallback does. Accepting only one was why clicking a sprite did nothing at all
      // — the hook was there, the two halves just never agreed on the word.
      const runId = selectedRunId(msg);
      if (runId) {
        // A character IS an agent now, so clicking one opens that agent and the errands it was
        // given, rather than dropping you into whichever single run happened to speak for it.
        // A READY character has no real run behind it — its id names the agent directly.
        if (runId.startsWith("decl:")) {
          void vscode.commands.executeCommand("galaius.agents.agent", runId.slice(5));
          return;
        }
        const run = readAgentRuns().find((r) => r.run_id === runId);
        const company = companyOf(readOrg()) ?? undefined;
        const who = run ? roleOf(run as never, company) : null;
        if (who && !who.plain) void vscode.commands.executeCommand("galaius.agents.agent", who.id);
        else void vscode.commands.executeCommand("galaius.agents.chat", runId);
        return;
      }
      // The roster shares this document now, so its buttons arrive here too. Routed through the
      // same railRoute the side-bar view used, so what a row can do is decided in one place and
      // an untrusted message still cannot name an arbitrary command.
      railRoute(msg, {
        run: (command) => void vscode.commands.executeCommand(command),
        // A row is a COLLEAGUE, so clicking it goes where clicking his character goes: the
        // agent and its errands. Dropping straight into one run was exactly the navigation
        // the sprite handler above already rejects; a conversation is one level deeper.
        // Drilled-in rows ARE conversations, and your own sessions have no agent to open.
        open: (id) => {
          const run = readAgentRuns().find((r) => r.run_id === id);
          if (run && run.status !== "foreign" && !this.inside) {
            const company = companyOf(readOrg()) ?? undefined;
            const who = roleOf(run as never, company);
            if (!who.plain) {
              void vscode.commands.executeCommand("galaius.agents.agent", who.id);
              return;
            }
          }
          void vscode.commands.executeCommand("galaius.agents.chat", id);
        },
        agent: (id) => {
          this.inside = id;
          this.pushRoster();
          // "on the sidepanel, we should be able to view what TASKS an agent was given" — clicking
          // the role opens that depth beside the room rather than only narrowing the list here.
          if (id) void vscode.commands.executeCommand("galaius.agents.agent", id);
        },
        act: (command, id) => {
          const run = readAgentRuns().find((r) => r.run_id === id);
          if (run) void vscode.commands.executeCommand(command, { run });
        },
      });
    });
    this.watch();
    this.render();
  }

  /** Where the chosen roster view is remembered. "store in configs (cache) so it reuses the same
   *  next time" — the same memento the agents tree already keeps its grouping in, so a preference
   *  survives a window close without inventing a second place for UI state to live. */
  private static readonly VIEW_KEY = "galaius.workplace.rosterView";

  /** The chosen view, this session at minimum. Held in a FIELD as well as the memento: with only
   *  this.store?.update(...) a missing memento made the whole chooser inert — the write was
   *  swallowed by the optional chaining, the getter fell back, and every click re-rendered the
   *  same view. A preference that cannot be persisted must still be honoured while the panel is
   *  open; silently doing nothing is the one behaviour worth ruling out. */
  private chosenView: RosterView | undefined;

  private get rosterView(): RosterView {
    const stored = this.chosenView ?? this.store?.get<RosterView>(WorkplacePanel.VIEW_KEY);
    return ROSTER_VIEWS.some((v) => v.id === stored) ? stored as RosterView : "grouped";
  }

  /** One panel, reused — opening it twice should focus the room, not stack tabs of it. */
  public static show(log: vscode.OutputChannel, state?: vscode.Memento): void {
    if (WorkplacePanel.current) {
      WorkplacePanel.current.panel.reveal();
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      "galaius.workplace",
      "Galaius — Team",
      nextColumn() as vscode.ViewColumn,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    WorkplacePanel.current = new WorkplacePanel(panel, log, state);
  }

  /** What an agent can do, from its own definition file — cached, because the file changes only
   *  when the definition is edited and the building re-renders constantly. */
  private static readonly FACULTIES = new Map<string, string[]>();

  /** Everyone in the building right now, placed by what they are doing. */
  private state(): TeamState {
    return buildTeam(
      WorkplacePanel.withDeclared(scopeStore()?.runs() ?? readAgentRuns()) as never,
      // The whole recent window, not one event: the vendor emits housekeeping constantly, and a
      // finished worker's room is found by walking back to the last thing it actually did.
      (runId) => readAgentActivity(runId, STEP_WINDOW),
      Date.now() / 1000,
      readAgentMessages() as never,
      // What each one can DO, read from its own definition file. The parsing lives in
      // capabilities.ts and the filesystem read lives HERE, at the edge, so teamState stays pure.
      (run) => WorkplacePanel.facultiesOfDefinition(run.definition_path),
      // Which DOMAIN each one belongs to. The company file has declared these rooms all along —
      // Quality & Critics, Production & Makers, Research, Records, the Finance Desk — and nothing
      // placed anybody by them, so a finance agent stood among the code reviewers.
      (agent) => WorkplacePanel.departmentOf(agent),
      // WHO each run was held with. One body per agent: five sessions recorded as claude are the
      // coordinator five times, not five teammates, and an agent given three errands is one
      // colleague — which is what "I can see multiple 'claude' agents... they're all duplicates"
      // was looking at.
      (run) => {
        const company = companyOf(readOrg()) ?? undefined;
        return {
          id: roleOf(run as never, company).id,
          label: agentLabel(run as never, company),
        };
      },
    );
  }

  /** The department a definition is filed under, from the company file. Cached with the
   *  faculties, for the same reason: the file changes when someone edits the org, not per frame. */
  private static departmentOf(agent: string): { id: string; room?: string | null } | null {
    const org = readOrg();
    if (!org) return null;
    const seat = org.agents.find((a) => a.name === agent);
    if (!seat?.department) return null;
    const dept = org.departments.find((d) => d.id === seat.department);
    return { id: seat.department, room: dept?.room ?? null };
  }

  /** Everything else is pure; the one filesystem read for capabilities lives here. */
  private static facultiesOfDefinition(path: string | null | undefined): string[] {
    if (!path) return [];
    const cached = WorkplacePanel.FACULTIES.get(path);
    if (cached) return cached;
    let found: string[] = [];
    try {
      found = facultiesOf(parseCapabilities(fs.readFileSync(path, "utf8")));
    } catch {
      found = []; // a moved or unreadable definition must not take the building down
    }
    WorkplacePanel.FACULTIES.set(path, found);
    return found;
  }

  /** Re-draw if the building is on screen — the workspace switcher has to reach it too, or the
   *  tree changes workspace and the building carries on showing the old team. */
  public static refreshIfOpen(): void {
    WorkplacePanel.current?.render();
  }

  /** Whether the document exists yet. Assigning webview.html REBUILDS it — every sprite becomes
   *  a new element, every running animation dies, and there is no clock — so it happens once. */
  private mounted = false;
  /** The agent whose conversations the roster is narrowed to, if you have gone into one. */
  private inside: string | null = null;

  /** The roster, as a fragment for the panel beside the room.
   *
   *  It used to be a side-bar view. "Your team and agents panel are still on the left side, whereas
   *  they should be in the big main panel somewhere" — the room and the roster are two views of one
   *  company, so they share a surface, and the side bar is left for the conversation.
   */
  private roster(): string {
    const company = companyOf(readOrg()) ?? undefined;
    const store = scopeStore();
    const runs = WorkplacePanel.withDeclared(store?.runs() ?? readAgentRuns());
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const now = Date.now() / 1000;
    const rail = buildRail(
      runs,
      store ? describeScope(store.scope, folder ? projectFor(folder) : "") : "",
      // Idleness is time since the run was last OBSERVED doing something, not since it started.
      (run) => Math.max(0, now - (lastObservedAt(readAgentActivity(run.run_id, 40)) ?? now)),
      undefined,
      this.inside ? { agent: this.inside, roleOf: (r) => roleOf(r as never, company).id } : undefined,
      // The same identity the MAP draws with — so the roster's rows are the sprites, one unit.
      (r) => ({ id: roleOf(r as never, company).id, label: agentLabel(r as never, company) }),
      // The clock: old finishes fold to the ledger instead of resting on the surface — the
      // panel led with 8-day-old smoke probes while the living company sat below the fold.
      now,
    );
    // Asked BY NAME, because every run names a model the ranking does not carry (sonnet,
    // claude-sonnet-5): only a named ask resolves it, and the board on disk fills the rest.
    this.ensureCompetence(modelsOnScreen(rail));
    return railBody(
      rail,
      voiceOf,
      (run) => conversationTitle(run as never),
      (run) => ({ id: roleOf(run as never, company).id, label: agentLabel(run as never, company) }),
      (run) => actionsFor(run as never),
      (run) => {
        const model = (run as { model?: string }).model;
        const full = this.measured.competence(model);
        const text = shortCompetence(full);
        if (full && text) {
          return { text, title: `${model} — ${full} — one measure, never a verdict` };
        }
        // Never a bare blank: "could not ask" and "not ranked" look identical in an empty cell,
        // and the first of the two is a fact about this machine the reader can act on.
        return this.measured.unavailable
          ? { text: "—", title: this.measured.unavailable }
          : undefined;
      },
      (run) => (run as { model?: string }).model,
      this.rosterView,
    );
  }

  /** Remember the view and repaint the roster alone — the choice is a preference, not a reason to
   *  rebuild the panel. */
  private async chooseView(view: RosterView): Promise<void> {
    if (!ROSTER_VIEWS.some((v) => v.id === view)) return;  // untrusted: it arrives from a webview
    this.chosenView = view;
    this.pushRoster();                      // the view changes whether or not it can be REMEMBERED
    await this.store?.update(WorkplacePanel.VIEW_KEY, view);
  }

  /** Asked once for every surface that shows a number; never awaited by a paint. */
  private ensureCompetence(models: readonly string[]): void {
    void this.measured.ensure(models).then((known) => {
      if (known) WorkplacePanel.refreshIfOpen();
    });
  }

  /** Repaint just the roster — going into an agent must not rebuild the room and restart every
   *  animation in it. */
  private pushRoster(): void {
    void this.panel.webview.postMessage({ type: "roster", html: this.roster() });
  }

  /** The whole declared company, as synthetic READY runs for every agent with no real errand.
   *
   *  "We don't have all the agents! Some other agents exist but aren't used... rooms for all
   *  areas... Even all the finance agents... In any projects." The world drew only agents WITH
   *  runs, so a 13-member Finance Desk rendered as an empty room. The company file declares the
   *  roster; the world seats it — project-independent, because the company exists whether or not
   *  this folder has asked it anything yet.
   */
  private static withDeclared(runs: readonly import("./agents").AgentRun[]): import("./agents").AgentRun[] {
    const org = readOrg();
    if (!org) return [...runs];
    const company = companyOf(org) ?? undefined;
    const present = new Set(runs.map((r) => roleOf(r as never, company).id));
    const ready = org.agents
      .filter((a) => !present.has(a.name))
      .map((a) => ({
        run_id: `decl:${a.name}`,
        provider: a.providers?.[0] ?? "claude",
        name: a.title ?? a.name,
        agent: a.name,
        status: "declared",
      } as unknown as import("./agents").AgentRun));
    return [...runs, ...ready];
  }

  private render(): void {
    const state = this.state();
    // Once the document is up, push the new scene into it instead of replacing it. This is the
    // difference between a slideshow and something you can watch: the engine keeps each body's
    // position and facing across the update, so a worker whose room changed WALKS there rather
    // than appearing in it.
    if (this.mounted) {
      const html = renderScene(state, this.log);
      if (html !== null) {
        void this.panel.webview.postMessage({ type: "team", html, state });
        void this.panel.webview.postMessage({ type: "roster", html: this.roster() });
        return;
      }
      // No scene renderer (an old or broken bundle): fall back to rebuilding rather than freezing.
      this.mounted = false;
    }
    try {
      this.panel.webview.html = renderWorkplace(state, nonce(), this.log, {
        style: railStyle(), body: this.roster(), script: railScript(),
      });
      this.mounted = true;
      // Straight after a rebuild, or the engine sits on the shell's snapshot until the next
      // registry write — which on a quiet team is minutes of a still picture.
      void this.panel.webview.postMessage({ type: "team", html: renderScene(state, this.log), state });
    } catch (err) {
      this.log.appendLine(`workplace render failed: ${err}`);
      this.panel.webview.html = `<!DOCTYPE html><body>${String(err)}</body>`;
      this.mounted = false;
    }
  }

  /** Follow the registry so the room moves as the team works. Short debounce on purpose: this is
   *  meant to be watched, and a second of lag reads as a frozen picture. */
  private watch(): void {
    try {
      this.watcher = fs.watch(agentsDir(), () => {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this.render(), 150);
      });
    } catch (err) {
      this.log.appendLine(`workplace is not following changes: ${err}`);
    }
  }

  private dispose(): void {
    releaseColumn("workplace");
    clearTimeout(this.timer);
    this.watcher?.close();
    this.watcher = undefined;
    WorkplacePanel.current = undefined;
  }
}

//: How far back to look for the step that says what someone is doing. Enough to see past a run
//: of housekeeping lines, small enough that a busy registry stays cheap to read per refresh.
const STEP_WINDOW = 12;

/** A fresh nonce per render: the CSP admits only scripts carrying it. */
function nonce(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}
