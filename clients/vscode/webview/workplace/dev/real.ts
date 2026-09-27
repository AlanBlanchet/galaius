/** Renders the workplace from the REAL registry — the panel's own pipeline, no fixture.
 *
 *  Every prior "verified" round on this view ran on a fixture, and four times on this project a
 *  fixture picked a convenient shape and certified a bug (see artist memory). This harness is the
 *  counter-instrument: it mirrors WorkplacePanel.state() line for line — the same readers, the
 *  same scope filter, the same discovered-session merge, the same identify/department/faculties
 *  callbacks — and hands the result to the SAME renderWorkplace the webview bundle exports. What
 *  it writes is what the user's panel draws, for whatever $HOME points at.
 *
 *      npx esbuild webview/workplace/dev/real.ts --bundle --outfile=/tmp/real.js \
 *        --format=cjs --platform=node --target=es2022
 *      HOME=/isolated/home node /tmp/real.js <outdir> [--project interact] [--discovered dump.json]
 *
 *  --project reproduces the panel's default scope (kind:"current" for the folder the user has
 *  open); omit it for "all workspaces". --discovered is a saved interact agents discovered
 *  stdout, merged exactly as ScopeStore.runs() merges it.
 */
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { readAgentActivity, readAgentMessages, readAgentRuns } from "../../../src/agents";
import { actionsFor } from "../../../src/agentActions";
import { facultiesOf, parseCapabilities } from "../../../src/capabilities";
import { mergeDiscovered, parseDiscovered } from "../../../src/discovered";
import { companyOf, readOrg } from "../../../src/org";
import { buildRail } from "../../../src/rail";
import { railBody, railScript, railStyle } from "../../../src/railHtml";
import { agentLabel, conversationTitle, roleOf } from "../../../src/roster";
import { voiceOf } from "../../../src/statusLanguage";
import { buildTeam, lastObservedAt } from "../../../src/teamState";
import { scopeRuns } from "../../../src/workspaceScope";
import { renderWorkplace } from "../index";

// The panel's own constant (workplacePanel.ts STEP_WINDOW).
const STEP_WINDOW = 12;

// The real Dark+ / Light+ variables, same as dev/preview.ts.
const DARK = `
--vscode-editor-background:#1f1f1f;--vscode-editor-foreground:#cccccc;--vscode-foreground:#cccccc;
--vscode-descriptionForeground:#9d9d9d;--vscode-widget-border:#3c3c3c;--vscode-focusBorder:#0078d4;
--vscode-charts-blue:#4daafc;--vscode-charts-purple:#b180d7;--vscode-charts-yellow:#d7ba7d;
--vscode-charts-green:#89d185;--vscode-charts-orange:#d18616;--vscode-charts-red:#f14c4c;
--vscode-errorForeground:#f14c4c;
--vscode-font-family:'Segoe WPC','Segoe UI',system-ui,-apple-system,sans-serif;`;
const LIGHT = `
--vscode-editor-background:#ffffff;--vscode-editor-foreground:#3b3b3b;--vscode-foreground:#3b3b3b;
--vscode-descriptionForeground:#767676;--vscode-widget-border:#d4d4d4;--vscode-focusBorder:#0090f1;
--vscode-charts-blue:#1a85ff;--vscode-charts-purple:#652d90;--vscode-charts-yellow:#b89500;
--vscode-charts-green:#388a34;--vscode-charts-orange:#d18616;--vscode-charts-red:#e51400;
--vscode-errorForeground:#e51400;
--vscode-font-family:'Segoe WPC','Segoe UI',system-ui,-apple-system,sans-serif;`;

function themed(html: string, vars: string, klass: string): string {
  return html
    .replace("</head>", `<style>:root{${vars}}</style></head>`)
    .replace("<body>", `<body class="${klass}">`);
}

function arg(flag: string): string | null {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? (process.argv[i + 1] ?? null) : null;
}

const outDir = process.argv[2];
if (!outDir || outDir.startsWith("--")) throw new Error("usage: real.js <outdir> [--project name] [--discovered file]");
mkdirSync(outDir, { recursive: true });

const discoveredFile = arg("--discovered");
const discovered = discoveredFile ? parseDiscovered(readFileSync(discoveredFile, "utf8")) : [];
const all = mergeDiscovered(readAgentRuns(), discovered);
const project = arg("--project");
const scoped = scopeRuns(all, project ? { kind: "project", name: project } : { kind: "all" }, "");

const org = readOrg();

/** The whole declared company, exactly as WorkplacePanel.withDeclared() builds it: a synthetic
 *  READY run for every org agent with no real errand, so the world seats the roster the user
 *  actually declared — the panel renders ~40 bodies on an 18-run registry, and a harness showing
 *  only the run-backed ten would certify a different picture than the one he sees. */
function withDeclared(runs: readonly ReturnType<typeof readAgentRuns>[number][]): typeof runs {
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
    }) as unknown as (typeof runs)[number]);
  return [...runs, ...ready];
}
const runs = withDeclared(scoped);
const company = companyOf(org) ?? undefined;
const state = buildTeam(
  runs as never,
  (runId) => readAgentActivity(runId, STEP_WINDOW),
  Date.now() / 1000,
  readAgentMessages() as never,
  (run) => {
    const path = (run as { definition_path?: string | null }).definition_path;
    if (!path) return [];
    try {
      return facultiesOf(parseCapabilities(readFileSync(path, "utf8")));
    } catch {
      return [];
    }
  },
  (agent) => {
    if (!org) return null;
    const seat = org.agents.find((a) => a.name === agent);
    if (!seat?.department) return null;
    const dept = org.departments.find((d) => d.id === seat.department);
    return { id: seat.department, room: dept?.room ?? null };
  },
  (run) => ({ id: roleOf(run as never, company).id, label: agentLabel(run as never, company) }),
);

writeFileSync(join(outDir, "state.json"), JSON.stringify(state, null, 2));
/* The ROSTER beside the room, exactly as WorkplacePanel.roster() builds it — the toolbar the
   sweep reported unresponsive lives here, so the harness must carry it too. */
const now = Date.now() / 1000;
const rail = buildRail(
  runs as never,
  project ?? "all workspaces",
  (run) => Math.max(0, now - (lastObservedAt(readAgentActivity(run.run_id, 40)) ?? now)),
  undefined,
  undefined,
  // The SAME identity the panel groups by. Without it the harness rendered run-rows beside an
  // agent-map, and the header said "16 done" over a floor showing 8 — the mirror must mirror.
  (run) => ({ id: roleOf(run as never, company).id, label: agentLabel(run as never, company) }),
  // The same clock too: the ledger folding is the whole point of judging on real data.
  now,
);
const aside = {
  style: railStyle(),
  body: railBody(
    rail,
    voiceOf,
    (run) => conversationTitle(run as never),
    (run) => ({ id: roleOf(run as never, company).id, label: agentLabel(run as never, company) }),
    (run) => actionsFor(run as never),
  ),
  script: railScript(),
};
const doc = renderWorkplace(state, "devnonce123", aside);
writeFileSync(join(outDir, "real-dark.html"), themed(doc, DARK, "vscode-dark"));
writeFileSync(join(outDir, "real-light.html"), themed(doc, LIGHT, "vscode-light"));
console.log(
  `real: ${runs.length} runs -> ${state.workers.length} workers, ` +
  `scope=${project ?? "all"}, home=${process.env.HOME}`,
);
