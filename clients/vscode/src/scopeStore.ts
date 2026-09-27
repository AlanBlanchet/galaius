/** The one place that knows which workspace the panels are showing.
 *
 *  Deliberately shared rather than duplicated: the tree, the workplace and the sequence view each
 *  read runs independently today, and two copies of a rule drifting is precisely how the pod
 *  colours ended up disagreeing between the two panels. One store, one answer.
 */
import * as vscode from "vscode";

import { readAgentRuns, type AgentRun } from "./agents";
import { projectFor, scopeRuns, describeScope, scopeChoices, type Scope } from "./workspaceScope";
import { mergeDiscovered, parseDiscovered, isFresh, type Discovery } from "./discovered";
import { interactCli } from "./interactCli";

const SCOPE_KEY = "interact.agents.scope";

export class ScopeStore {
  private readonly changed = new vscode.EventEmitter<void>();
  /** Fires when the scope changes, so every view re-reads rather than showing a stale workspace. */
  readonly onDidChange = this.changed.event;

  constructor(
    private readonly memento: vscode.Memento,
    /** Where a discovery failure gets said out loud. Optional so a test can build a store
     *  without one; the panel always passes its channel, reachable from the chat as /logs. */
    private readonly log?: vscode.OutputChannel,
  ) {}

  /** The last answer from interact agents discovered, and when it arrived. */
  private discovered: Discovery | null = null;
  private discovering = false;

  get scope(): Scope {
    return this.memento.get<Scope>(SCOPE_KEY, { kind: "current" });
  }

  /** The project the open folder belongs to, by the same rule the registry stamps onto a run. */
  get currentProject(): string {
    const folder = vscode.workspace.workspaceFolders?.[0];
    return folder ? projectFor(folder.uri.fsPath) : "";
  }

  get workspaceFolders(): string[] {
    return (vscode.workspace.workspaceFolders ?? []).map(({ uri }) => uri.fsPath);
  }

  /** Every run the panel should be showing — including the sessions interact didn't start.
   *
   *  Those have no record on disk, so a directory read alone never saw them: the panel couldn't
   *  show the user's own editor windows, nor offer the folders they're working in. Kept SYNC by
   *  serving the last discovery and refreshing behind it — a subprocess on every repaint to
   *  track something that changes when a person opens a window would be the wrong trade.
   */
  runs(): AgentRun[] {
    this.refreshDiscovery();
    const all = mergeDiscovered(readAgentRuns(), this.discovered?.runs ?? []);
    return scopeRuns(all, this.scope, this.currentProject, this.workspaceFolders);
  }

  /** Re-ask for foreign sessions when the last answer has aged out. Fire-and-forget: the current
   *  call serves what we already have, and a failure leaves the panel exactly as it was. */
  private refreshDiscovery(): void {
    if (this.discovering || isFresh(this.discovered, Date.now())) return;
    this.discovering = true;
    void interactCli(["agents", "discovered"])
      .then(({ stdout, error }) => {
        // A stamp on failure too, so a machine without the CLI on PATH is not re-probed on every
        // repaint — it retries on the same slow cadence as a success.
        this.discovered = { runs: error ? (this.discovered?.runs ?? []) : parseDiscovered(stdout),
                            at: Date.now() };
        // Said once per failure rather than swallowed. A silent failure is indistinguishable from
        // "you have no other sessions", which is a wrong answer wearing a plausible face.
        if (error) this.log?.appendLine(`interact agents discovered: ${error}`);
        else this.changed.fire();
      })
      // The flag is cleared in finally, never only on the happy path: leaving it set disables
      // discovery for the life of the window, and the panel then serves one stale answer forever
      // with nothing on screen to say so. Assigned BEFORE changed.fire() above for the same
      // reason — the fire re-enters runs(), which calls this.
      .finally(() => { this.discovering = false; });
  }

  /** What the panel is showing, for a title or a status line. */
  describe(): string {
    return describeScope(this.scope, this.currentProject);
  }

  /** Offer the switcher and apply the answer. Returns whether the scope moved. */
  async pick(): Promise<boolean> {
    // Built from the SAME merged set the tree shows, so a folder whose only agents are your own
    // editor windows is still offered — that folder is exactly the one you cannot otherwise reach.
    const choices = scopeChoices(
      mergeDiscovered(readAgentRuns(), this.discovered?.runs ?? []), this.currentProject);
    const chosen = await vscode.window.showQuickPick(
      choices.map((c) => ({ label: c.label, detail: c.detail, scope: c.scope })),
      { title: "Show agents from", placeHolder: this.describe() },
    );
    if (!chosen) return false;
    await this.memento.update(SCOPE_KEY, chosen.scope);
    this.changed.fire();
    return true;
  }
}


/** The one live store.
 *
 *  Held here rather than passed into each view because the workplace panel is imported lazily —
 *  it cannot be handed anything at activation time. A single accessor also makes it impossible
 *  for two views to end up holding different stores, which is the failure this whole module
 *  exists to prevent.
 */
let current: ScopeStore | undefined;

export function setScopeStore(store: ScopeStore): void {
  current = store;
}

export function scopeStore(): ScopeStore | undefined {
  return current;
}
