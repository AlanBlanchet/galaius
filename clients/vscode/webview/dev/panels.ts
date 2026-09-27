/** Renders the SIDE PANEL's documents as plain pages, so they can be opened in a browser and
 *  tested without VS Code.
 *
 *  This used to live in /tmp, written by hand during a review. Four tests depended on it, so
 *  when /tmp was cleaned they stopped running — silently, reported as "the chat render fixture
 *  is not present", reading like a deliberate skip rather than a guard that quietly went. A test
 *  whose fixture lives outside the repo is a test that stops guarding without telling anyone.
 *
 *      node out-panels.js <outdir>
 *
 *  A webview IS a browser; the only things missing outside VS Code are the theme variables the
 *  editor injects and the acquireVsCodeApi handle, both supplied here from the real values so
 *  what renders in the harness is what renders in the panel.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { chatDocument } from "../../src/conversationFormat";
import { CHAT_COMMANDS } from "../../src/chatCommands";
import { railBody, railDocument, railStyle } from "../../src/railHtml";
import { buildRail } from "../../src/rail";
import { voiceOf } from "../../src/statusLanguage";
import { conversationTitle, roleOf } from "../../src/roster";
import { actionsFor } from "../../src/agentActions";

/** The theme variables VS Code injects on <body> — real values, not approximations. */
const THEMES: Record<string, Record<string, string>> = {
  dark: {
    "--vscode-font-family": "system-ui, sans-serif",
    "--vscode-editor-background": "#1f1f1f",
    "--vscode-editor-foreground": "#cccccc",
    "--vscode-foreground": "#cccccc",
    "--vscode-sideBar-background": "#181818",
    "--vscode-descriptionForeground": "#9d9d9d",
    "--vscode-panel-border": "#2b2b2b",
    "--vscode-focusBorder": "#0078d4",
    "--vscode-list-hoverBackground": "#2a2d2e",
    "--vscode-textLink-foreground": "#4daafc",
    "--vscode-charts-red": "#f14c4c",
    "--vscode-charts-blue": "#3794ff",
    "--vscode-charts-green": "#89d185",
    "--vscode-charts-yellow": "#cca700",
    "--vscode-charts-purple": "#b180d7",
    "--vscode-editor-font-family": "monospace",
  },
  light: {
    "--vscode-font-family": "system-ui, sans-serif",
    "--vscode-editor-background": "#ffffff",
    "--vscode-editor-foreground": "#3b3b3b",
    "--vscode-foreground": "#3b3b3b",
    "--vscode-sideBar-background": "#f8f8f8",
    "--vscode-descriptionForeground": "#767676",
    "--vscode-panel-border": "#e5e5e5",
    "--vscode-focusBorder": "#005fb8",
    "--vscode-list-hoverBackground": "#e8e8e8",
    "--vscode-textLink-foreground": "#005fb8",
    "--vscode-charts-red": "#e51400",
    "--vscode-charts-blue": "#1a85ff",
    "--vscode-charts-green": "#388a34",
    "--vscode-charts-yellow": "#b89500",
    "--vscode-charts-purple": "#652d90",
    "--vscode-editor-font-family": "monospace",
  },
};

/** Wrap a document so it renders outside the editor: theme variables on body, exactly where VS
 *  Code puts them, and a no-op API handle so the page's own script runs instead of throwing. */
function standalone(html: string, theme: string): string {
  const vars = Object.entries(THEMES[theme]).map(([k, v]) => `${k}:${v}`).join(";");
  return html
    .replace("<body>", `<body style="${vars}">`)
    // WITH the nonce: the document carries script-src 'nonce-…', so an un-nonced shim is
    // blocked by the page's own CSP, acquireVsCodeApi stays undefined, and the real script
    // throws on its first line — every handler unbound, looking exactly like a broken composer.
    .replace("<script nonce=", `<script nonce="N0NCE">window.acquireVsCodeApi=()=>({postMessage(){},getState(){},setState(){}});</script>\n<script nonce=`);
}

const RUNS = [
  { run_id: "run-error", provider: "claude", name: "code-reviewer", agent: "code-reviewer",
    task: "Review the panel diff for dead controls", status: "failed", started_at: 900 },
  { run_id: "run-work", provider: "claude", name: "researcher", agent: "researcher",
    task: "Find what the Ollama plan actually includes", status: "running", started_at: 800 },
  { run_id: "run-done", provider: "claude", name: "librarian", agent: "librarian",
    task: "Add the overlay rule to the prompt repo", status: "done", started_at: 700 },
];

const TURNS = [
  { kind: "message", text: "Check whether the fold runs before the parser.", at: 1 },
  { kind: "text", at: 2, text: [
      "## Verdict", "", "It does — and that is the defect.", "",
      "| Surface | Before | After |", "| --- | --- | --- |",
      "| chat | pipes | a table |", "", "> Parse first, fold second.",
    ].join("\n") },
];

function main(outDir: string): void {
  mkdirSync(join(outDir, "chat"), { recursive: true });
  mkdirSync(join(outDir, "rail"), { recursive: true });

  for (const theme of Object.keys(THEMES)) {
    const chat = chatDocument({
      nonce: "N0NCE",
      turns: TURNS as never[],
      // A RUNNING agent on purpose: the composer refuses to send to one that has ended, so a
      // finished fixture would test the refusal rather than the send, and the tests that matter
      // here are about not losing typed text.
      name: "researcher",
      status: "running",
      awaitingReply: false,
      run: { run_id: "run-work", provider: "claude", agent: "researcher", status: "running" } as never,
      files: [],
      sentBy: "you",
      commands: CHAT_COMMANDS as never[],
      parent: { runId: "run-done", title: "Add the overlay rule to the prompt repo" },
    });
    writeFileSync(join(outDir, "chat", `${theme}.html`), standalone(chat, theme));

    const rail = buildRail(RUNS as never[], "interact", () => 0);
    const railDoc = railDocument("N0NCE", railStyle(), railBody(
      rail, voiceOf,
      (r) => conversationTitle(r as never),
      (r) => ({ id: roleOf(r as never).id, label: roleOf(r as never).id }),
      (r) => actionsFor(r as never),
    ));
    writeFileSync(join(outDir, "rail", `${theme}.html`), standalone(railDoc, theme));
  }
  console.log(`wrote chat/ and rail/ pages for ${Object.keys(THEMES).join(", ")} into ${outDir}`);
}

main(process.argv[2] ?? "/tmp");
