/** The Agents panel's pure decisions — no vscode import, so they're unit-testable.
 *
 *  Same discipline as paths.ts: anything decidable without the extension host lives here, pinned
 *  by agentsFormat.test.ts — the panel's whole job is to tell the truth at a glance, and those
 *  are exactly the judgements that go quietly wrong.
 */
import * as path from "path";

import type { AgentRun } from "./agents";

export type GroupBy = "project" | "provider" | "model" | "flat";

export interface StatusIcon {
  id: string;
  color?: string;
}

/** Icon + theme colour per status.
 *
 *  running spins, so a live agent is obvious from the corner of the eye — the entire reason to
 *  have a sidebar rather than a tab. Nothing else spins: a spinner on a finished run claims work
 *  is happening when it isn't.
 *
 *  foreign has its OWN colour rather than sharing stopped's. The two differed by glyph alone, so
 *  one of your own live sessions read at a glance as the same tier as a dead run. Foreign is
 *  about OWNERSHIP — not ours to drive — never about being finished.
 */
const STATUS_ICON: Record<string, StatusIcon> = {
  running: { id: "sync~spin", color: "charts.blue" },
  done: { id: "pass-filled", color: "charts.green" },
  failed: { id: "error", color: "charts.red" },
  crashed: { id: "warning", color: "charts.orange" },
  stopped: { id: "circle-slash", color: "descriptionForeground" },
  foreign: { id: "circle-outline", color: "charts.purple" },
};

/** How a status should be drawn. An unknown one falls back to the foreign icon rather than
 *  nothing: a row with no icon reads as a rendering fault, not as an unrecognised state. */
export function statusIcon(status: string): StatusIcon {
  return STATUS_ICON[status] ?? STATUS_ICON.foreign;
}



/** API-equivalent usage value. null is unknown and renders an em dash; it never implies a
 *  provider charge, account coverage, or zero account impact. */
export function formatCost(cost: number | null | undefined): string {
  return cost == null ? "—" : `~$${cost.toFixed(4)}`;
}

/** Time on the clock: counting up while it runs, frozen at its end once it has one. */
export function formatElapsed(run: AgentRun): string {
  const start = (run.started_at ?? 0) * 1000;
  if (!start) return "";
  const end = run.finished_at ? run.finished_at * 1000 : Date.now();
  const s = Math.max(0, Math.floor((end - start) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function groupKeyFor(run: AgentRun, by: GroupBy): string {
  // Prefer the project Python derived (repo root). Falling back to directory name keeps records
  // written before that field existed grouping sensibly, not vanishing.
  if (by === "project") {
    return run.project || (run.cwd ? path.basename(run.cwd) : "(no project)");
  }
  if (by === "provider") return run.provider || "(unknown)";
  if (by === "model") return run.model || "(default model)";
  return "";
}

/** Live work first. A sidebar you must scroll to find the running agent in has failed at its one
 *  job; ties fall back to name so the order is stable between refreshes. */
export function orderGroups(groups: [string, AgentRun[]][]): [string, AgentRun[]][] {
  const live = (rs: AgentRun[]) => rs.filter((r) => r.status === "running").length;
  return [...groups].sort((a, b) => live(b[1]) - live(a[1]) || a[0].localeCompare(b[0]));
}


/** Max characters a row's description may occupy.
 *
 *  Side bar is narrow, so anything past roughly this is clipped by VS Code with no say in WHERE
 *  — how every row ended up reading "→ 2b7642ee: Reply wit…". Clipping here instead lands the
 *  cut on a word boundary, ellipsis ours.
 */
const ROW_WIDTH = 72;

/** What a DISCOVERED session reports in last — how it's being driven, not what it's doing.
 *  Rendered as-is, it put a transport label in the column where every other row carries an
 *  activity ("delegating the workplace view…"), so a foreign row read "interactive" and the eye
 *  parsed it as work that never happened. */
const TRANSPORT_LABELS = new Set(["interactive", "background"]);

/** What a run's row says beside its name.
 *
 *  The last thing that happened gets the whole width when there IS one: elapsed time and cost
 *  are already on hover and the dashboard, so competing with them for a narrow row meant the
 *  interesting half — what the agent actually did — was the half that got cut.
 */
export function rowDescription(run: {
  last?: string | null;
  status: string;
  cost_usd?: number | null;
  foreign?: boolean;
}): string {
  const said = (run.last ?? "").trim();
  if (run.foreign && (!said || TRANSPORT_LABELS.has(said.toLowerCase()))) {
    // Says whose it is instead. That IS the useful fact about a session interact didn't start:
    // you can't drive it, and it's not part of the team's work or its cost.
    return "your own session";
  }
  if (!said) return `${run.status} · ${formatCost(run.cost_usd)}`;
  if (said.length <= ROW_WIDTH) return said;
  const cut = said.slice(0, ROW_WIDTH);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > ROW_WIDTH / 2 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}
