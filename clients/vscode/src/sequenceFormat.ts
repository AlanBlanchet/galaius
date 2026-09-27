/** The sequence view: who talked to whom, and when.
 *
 *  A tree answers "what is running"; a transcript answers "what did ONE agent do". Neither shows
 *  a TEAM — that a reviewer is waiting on a lead, that a spawn happened before a message. So:
 *  one lane per agent, time flowing downward, an arrow for every spawn and every message. It's
 *  the standard shape for interacting participants because it carries three facts at once —
 *  who, what, and in what order — which a node-graph loses (no time) and a plain log loses (no
 *  participants).
 *
 *  Pure layout, no vscode import, so the decisions that go quietly wrong here are unit-tested.
 */

export interface SeqRun {
  run_id: string;
  name: string;
  provider: string;
  status: string;
  started_at?: number;
  parent_run_id?: string | null;
}

export interface SeqMessage {
  from_run: string;
  to_run: string;
  text: string;
}

export interface Lane {
  run_id: string;
  name: string;
  provider: string;
  status: string;
  x: number;
}

export interface Arrow {
  kind: "spawn" | "message";
  fromLane: number;
  toLane: number;
  label: string;
  y: number;
}

export interface Sequence {
  lanes: Lane[];
  arrows: Arrow[];
  width: number;
  height: number;
  /** Agents in scope who neither spawned, were spawned, nor spoke — shown as a count, never as
   *  empty columns. */
  silent: number;
}

const LANE_W = 190;
const LANE_X0 = 90;
const ROW_H = 46;
const TOP = 74;

/** Lanes in START order — the diagram reads as the team forming, left to right. */
export function buildSequence(runs: SeqRun[], messages: SeqMessage[]): Sequence {
  // A lane is EARNED by participating — spawning, being spawned, or exchanging a message. The
  // whole roster used to get one each: seventeen columns for two participants, the subject hidden
  // by its own cast list. When nobody interacted at all, everyone keeps a lane: a team that never
  // spoke is still a team, and an empty stage reads as a bug.
  const spoke = new Set<string>();
  for (const r of runs) {
    if (r.parent_run_id) { spoke.add(r.run_id); spoke.add(r.parent_run_id); }
  }
  for (const m of messages) { spoke.add(m.from_run); spoke.add(m.to_run); }
  const cast = spoke.size ? runs.filter((r) => spoke.has(r.run_id)) : runs;
  const silent = runs.length - cast.length;
  const ordered = [...cast].sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0));
  const lanes: Lane[] = ordered.map((r, i) => ({
    run_id: r.run_id,
    name: r.name,
    provider: r.provider,
    status: r.status,
    x: LANE_X0 + i * LANE_W,
  }));
  const laneOf = new Map(lanes.map((l, i) => [l.run_id, i]));

  const arrows: Arrow[] = [];
  // Spawns first: a team forms before it talks, and a child's lane exists because a parent made it.
  for (const run of ordered) {
    if (!run.parent_run_id) continue;
    const from = laneOf.get(run.parent_run_id);
    const to = laneOf.get(run.run_id);
    // An arrow with no landing lane is DROPPED. Pointing it at lane 0 would render as an agent
    // talking to itself — a fabricated relationship, which is worse than an absent one.
    if (from === undefined || to === undefined || from === to) continue;
    arrows.push({ kind: "spawn", fromLane: from, toLane: to, label: "spawned", y: 0 });
  }
  for (const msg of messages) {
    const from = laneOf.get(msg.from_run);
    const to = laneOf.get(msg.to_run);
    if (from === undefined || to === undefined) continue;
    arrows.push({
      kind: "message",
      fromLane: from,
      toLane: to,
      label: msg.text.replace(/\s+/g, " ").slice(0, 60),
      y: 0,
    });
  }
  // Order IS the meaning here, so y is assigned after collection, never per-category.
  arrows.forEach((a, i) => {
    a.y = TOP + i * ROW_H;
  });

  return {
    lanes,
    arrows,
    silent,
    width: LANE_X0 + Math.max(1, lanes.length) * LANE_W,
    height: TOP + Math.max(1, arrows.length) * ROW_H + 30,
  };
}

function esc(v: string): string {
  return v
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** The same mix as themeTokens.DIM_FOREGROUND, spelled out rather than imported.
 *
 *  This module is loaded directly by sequence.test.ts under --experimental-strip-types, which
 *  needs an explicit ".ts" specifier on every import, while tsc refuses one when it emits. So a
 *  file the tests reach this way stays import-free, and the theme tokens live beside the status
 *  colours below, already spelled here for the same reason. themeTokens.test.ts fails if the
 *  two copies ever drift.
 */
const DIM_FOREGROUND =
  "color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 70%, " +
  "var(--vscode-editor-foreground, #d4d4d4))";

const STATUS_COLOR: Record<string, string> = {
  running: "var(--vscode-charts-blue)",
  done: "var(--vscode-charts-green)",
  failed: "var(--vscode-charts-red)",
  crashed: "var(--vscode-charts-orange)",
};

/** The diagram as inline SVG. Colours are theme variables so it works in light and dark, and the
 *  arrowhead is drawn per-arrow rather than via a shared <marker> — a marker inherits the
 *  marker element's own colour, not the line's, so a themed stroke would silently lose its head. */
export function renderSequence(seq: Sequence): string {
  if (seq.lanes.length === 0) {
    return '<p class="empty">No agents yet. Spawn one and the exchanges appear here.</p>';
  }
  const parts: string[] = [];
  for (const lane of seq.lanes) {
    const color = STATUS_COLOR[lane.status] ?? DIM_FOREGROUND;
    parts.push(
      `<line class="lifeline" x1="${lane.x}" y1="58" x2="${lane.x}" y2="${seq.height - 12}"/>`,
      `<circle cx="${lane.x}" cy="30" r="5" fill="${color}"/>`,
      `<text class="lane-name" x="${lane.x}" y="20" text-anchor="middle">${esc(lane.name)}</text>`,
      `<text class="lane-meta" x="${lane.x}" y="50" text-anchor="middle">${esc(lane.provider)}</text>`,
    );
  }
  for (const arrow of seq.arrows) {
    const x1 = seq.lanes[arrow.fromLane].x;
    const x2 = seq.lanes[arrow.toLane].x;
    const dir = x2 > x1 ? -1 : 1;
    const head = `${x2 + dir * 8},${arrow.y - 4} ${x2},${arrow.y} ${x2 + dir * 8},${arrow.y + 4}`;
    const cls = arrow.kind === "spawn" ? "spawn" : "message";
    const mid = (x1 + x2) / 2;
    parts.push(
      `<line class="arrow ${cls}" x1="${x1}" y1="${arrow.y}" x2="${x2}" y2="${arrow.y}"/>`,
      `<polyline class="head ${cls}" points="${head}"/>`,
      `<text class="arrow-label ${cls}" x="${mid}" y="${arrow.y - 7}" text-anchor="middle">${esc(arrow.label)}</text>`,
    );
  }
  // The silent are a sentence, not columns: "15 silent" tells the reader the cast was bigger
  // without hiding the two who actually spoke behind fifteen empty lanes.
  const silent = seq.silent > 0
    ? `<text x="${seq.width - 12}" y="${18}" text-anchor="end" class="seq-silent">${seq.silent} silent</text>`
    : "";
  return `<svg viewBox="0 0 ${seq.width} ${seq.height}" width="${seq.width}" height="${seq.height}" role="img" aria-label="Agent sequence">${parts.join("")}${silent}</svg>`;
}
