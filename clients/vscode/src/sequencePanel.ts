/** The Agent sequence panel — the team, not one agent.
 *
 *  Opens beside your code and follows the registry, so a spawn or a message appears as it
 *  happens. Read-only and script-free: agent text is untrusted, and a diagram is looked at, not
 *  driven.
 */
import * as fs from "fs";
import * as vscode from "vscode";

import { readAgentMessages, readAgentRuns } from "./agents";
import { agentLabel } from "./roster";
import { companyOf, readOrg } from "./org";
import { scopeStore } from "./scopeStore";
import { agentsDir } from "./paths";
import { buildSequence, renderSequence } from "./sequenceFormat";
import { DIM_FOREGROUND } from "./themeTokens";
import { claimColumn, nextColumn, releaseColumn } from "./panelColumn";

export class SequencePanel {
  private static current: SequencePanel | undefined;
  private watcher: fs.FSWatcher | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;

  private constructor(private readonly panel: vscode.WebviewPanel) {
    this.panel.onDidDispose(() => this.dispose());
    // One column for interact's surfaces: a new one joins the group its siblings already
    // hold rather than opening yet another beside your code.
    claimColumn("sequence", this.panel.viewColumn);
    try {
      this.watcher = fs.watch(agentsDir(), () => {
        if (this.timer) clearTimeout(this.timer);
        this.timer = setTimeout(() => this.render(), 300);
      });
      this.watcher.on("error", () => {});
    } catch {
      /* unwatchable → static diagram */
    }
    this.render();
  }

  static show(): void {
    if (SequencePanel.current) {
      SequencePanel.current.panel.reveal(SequencePanel.current.panel.viewColumn);
      SequencePanel.current.render();
      return;
    }
    SequencePanel.current = new SequencePanel(
      vscode.window.createWebviewPanel(
        "interact.sequence",
        "Agent sequence",
      nextColumn() as vscode.ViewColumn,
        { enableScripts: false, retainContextWhenHidden: true },
      ),
    );
  }

  private dispose(): void {
    releaseColumn("sequence");
    this.watcher?.close();
    if (this.timer) clearTimeout(this.timer);
    SequencePanel.current = undefined;
  }

  private render(): void {
    // Scoped, like the tree and the workplace. This read the whole machine, so switching
    // workspace left the sequence drawing another folder's conversation.
    const runs = (scopeStore()?.runs() ?? readAgentRuns())
      .filter((r) => !r.foreign); // your own editor windows are not team members
    // Lanes wear the AGENT'S name, the same one the world's plaque and the roster row carry.
    // Raw run names put "claude" over three different colleagues on one diagram — the exact
    // CLI-naming the roster banned, sneaking back through a surface that bypassed roster.ts.
    const company = companyOf(readOrg()) ?? undefined;
    const seq = buildSequence(
      runs.map((r) => ({ ...r, name: agentLabel(r as never, company) })) as never[],
      readAgentMessages());
    const live = runs.filter((r) => r.status === "running").length;
    this.panel.webview.html = `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<style>
 body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);
      background:var(--vscode-editor-background);padding:14px 18px}
 h1{font-size:14px;margin:0 0 2px}
 .meta{color:${DIM_FOREGROUND};font-size:12px;margin:0 0 14px}
 .empty{color:${DIM_FOREGROUND}}
 .scroll{overflow:auto;max-width:100%}
 .lifeline{stroke:var(--vscode-panel-border);stroke-width:1;stroke-dasharray:3 4}
 .lane-name{fill:var(--vscode-foreground);font-size:12px;font-weight:600}
 .lane-meta{fill:${DIM_FOREGROUND};font-size:10px}
 .arrow{stroke-width:1.4}
 .arrow.message{stroke:var(--vscode-charts-blue)}
 .arrow.spawn{stroke:var(--vscode-charts-purple);stroke-dasharray:4 3}
 .head{fill:none;stroke-width:1.4}
 .head.message{stroke:var(--vscode-charts-blue)}
 .head.spawn{stroke:var(--vscode-charts-purple)}
 .arrow-label{font-size:10px}
 .arrow-label.message{fill:var(--vscode-charts-blue)}
 .arrow-label.spawn{fill:var(--vscode-charts-purple)}
 .key{margin-top:14px;font-size:11px;color:${DIM_FOREGROUND}}
</style></head><body>
<h1>Agent sequence</h1>
<p class="meta">${seq.lanes.length} agent${seq.lanes.length === 1 ? "" : "s"} · ${live} running · time flows downward</p>
<div class="scroll">${renderSequence(seq)}</div>
<p class="key">— solid = a message · dashed = a spawn</p>
</body></html>`;
  }
}
