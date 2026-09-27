/** The tab strip above a conversation: everyone this session put to work.
 *
 *  "When we call an agent, while it's working, the main agent would / could do things, thus we
 *  need to make these things work well together, and make small tabs in the conversation so we
 *  can view how the agent is doing, if it's calling other agents etc.. And we should be able to
 *  come back to the main agent."
 *
 *  So: the entry agent is always the first tab (the way home is never more than one click), each
 *  agent it sent out is a tab, and an agent that sent out its OWN helpers shows them too —
 *  indented by depth, because "is it calling other agents" is the question the strip answers.
 *
 *  Pure: no vscode, no filesystem, no runtime import at all, so the decisions that go quietly
 *  wrong here are unit-tested (the test loader demands ".ts" specifiers tsc refuses to emit).
 */

import type { AgentRun } from "./agents";

export interface SessionTab {
  runId: string;
  label: string;
  /** 0 for the entry agent, 1 for somebody it sent out, 2 for their helper. */
  depth: number;
  /** The conversation you are looking at right now. */
  here: boolean;
  /** The entry agent — the way back. */
  root: boolean;
  /** Still working: the dot that makes the strip worth glancing at. */
  live: boolean;
  /** On the overflow tab only: how many colleagues it stands for. */
  more?: number;
}

/** How many tabs fit a side panel before the strip becomes a scrollbar. */
const CAP = 8;

/** Walk from a run up to the session's entry agent, then back down through everyone it put to
 *  work. label is passed in (this module stays import-free) — it's the agent's name, the same
 *  one the roster row and the world's plaque carry. */
export function sessionTabs(
  runs: readonly AgentRun[],
  hereId: string,
  label: (run: AgentRun) => string,
): SessionTab[] {
  const ours = runs.filter((r) => r.status !== "foreign" && r.status !== "declared");
  const byId = new Map(ours.map((r) => [r.run_id, r]));
  const here = byId.get(hereId);
  if (!here) return [];

  // The root of THIS session: walk up until nobody sent us.
  let root = here;
  const seen = new Set<string>([root.run_id]);
  while (root.parent_run_id) {
    const up = byId.get(root.parent_run_id);
    if (!up || seen.has(up.run_id)) break;
    seen.add(up.run_id);
    root = up;
  }

  // Everyone under the root, in the order they were put to work — depth-first, so an agent's own
  // helpers sit next to it rather than at the end of the strip.
  const kids = new Map<string, AgentRun[]>();
  for (const r of ours) {
    if (!r.parent_run_id || r.parent_run_id === r.run_id) continue;
    const list = kids.get(r.parent_run_id) ?? [];
    list.push(r);
    kids.set(r.parent_run_id, list);
  }
  const order: { run: AgentRun; depth: number }[] = [];
  const walk = (run: AgentRun, depth: number): void => {
    order.push({ run, depth });
    for (const kid of (kids.get(run.run_id) ?? [])
      .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0))) {
      if (depth < 6) walk(kid, depth + 1);
    }
  };
  walk(root, 0);

  // Alone on the errand: a strip with one tab says nothing a header does not.
  if (order.length < 2) return [];

  const tab = (o: { run: AgentRun; depth: number }): SessionTab => ({
    runId: o.run.run_id,
    label: label(o.run),
    depth: o.depth,
    here: o.run.run_id === hereId,
    root: o.depth === 0,
    live: o.run.status === "running",
  });

  if (order.length <= CAP) return order.map(tab);

  // Over the cap: keep the root, the conversation you are in, and the most recent colleagues —
  // then say plainly how many are not shown rather than silently dropping them.
  const keep = new Set<string>([root.run_id, hereId]);
  for (const o of [...order].reverse()) {
    if (keep.size >= CAP - 1) break;
    keep.add(o.run.run_id);
  }
  const shown = order.filter((o) => keep.has(o.run.run_id)).map(tab);
  return [...shown, {
    runId: "", label: `+${order.length - shown.length}`, depth: 0,
    here: false, root: false, live: false, more: order.length - shown.length,
  }];
}
