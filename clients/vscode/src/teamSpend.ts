/** What the team is costing, and the open agent's share.
 *
 *  The chat reported one agent's tokens and cost and nothing about the whole, which is the wrong
 *  half for someone who owns the budget: the actionable numbers are what the team is spending and
 *  whether the agent in front of you is a large part of it.
 *
 *  Cost here is an API-equivalent value only; it does not establish provider charge or account
 *  impact. The typed charge_path and cost_certainty facts own those billing semantics.
 */
export interface TeamSpendRun {
  run_id: string;
  cost_usd?: number | null;
  status?: string;
  foreign?: boolean;
}

export interface TeamSpend {
  total: number | null;
  agents: number;
  running: number;
  /** The open agent's share, or null when its own cost is not known — null is unknown, not
   *  zero, and reporting 0% would assert that this agent is free. */
  sharePercent: number | null;
}

export function teamSpend(
  runs: readonly TeamSpendRun[],
  openRunId: string | undefined,
  current: readonly TeamSpendRun[] = [],
): TeamSpend {
  // A foreign run is one of the user's own editor sessions: real spend, but not this team's, and
  // counting it would deflate every share below.
  const currentById = new Map(current.map((run) => [run.run_id, run]));
  const merged = runs.map((run) => mergeSpendRun(run, currentById.get(run.run_id)));
  const knownIds = new Set(merged.map((run) => run.run_id));
  merged.push(...current.filter((run) => !knownIds.has(run.run_id)));
  const ours = merged.filter((r) => !r.foreign && r.status !== "foreign");
  const reported = ours.flatMap((run) => run.cost_usd == null ? [] : [run.cost_usd]);
  const total = reported.length ? reported.reduce((sum, cost) => sum + cost, 0) : null;
  const open = ours.find((r) => r.run_id === openRunId);
  const share = open?.cost_usd != null && total != null && total > 0
    ? Math.round((open.cost_usd / total) * 100)
    : null;
  return {
    total,
    agents: ours.length,
    running: ours.filter((r) => r.status === "running").length,
    sharePercent: share,
  };
}

const TERMINAL = new Set(["done", "failed", "cancelled", "crashed", "stopped"]);

function mergeSpendRun(first: TeamSpendRun, second: TeamSpendRun | undefined): TeamSpendRun {
  if (!second) return first;
  const firstTerminal = TERMINAL.has(first.status ?? "");
  const secondTerminal = TERMINAL.has(second.status ?? "");
  return {
    ...first,
    ...second,
    status: firstTerminal && !secondTerminal ? first.status : second.status ?? first.status,
    cost_usd: second.cost_usd ?? first.cost_usd,
  };
}
