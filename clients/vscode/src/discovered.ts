/** The sessions interact did NOT start — your own editor windows — merged into the panel's view.
 *
 *  These have no record on disk. They are found by asking each provider at list time, so a panel
 *  that reads the registry DIRECTORY (this one does, and should: it is a file read, not a
 *  subprocess, on every refresh) could never see one — while carrying a status icon and a tooltip
 *  for them the whole time. "i have agents in the 'sheets' folder elsewhere, and i can't change
 *  and see how they work" was literally true: the panel had no way to learn they existed.
 *
 *  So the fast path stays a file read, and this adds the discovered ones from interact agents
 *  discovered on a slow cadence — editor windows open and close on a human timescale, not on a
 *  refresh timer, and paying a subprocess per repaint to track that would be the wrong trade.
 */
// import type on purpose: the test loader (node --test --experimental-strip-types) resolves
// real specifiers and would need a ".ts" suffix that tsc then refuses to emit. A type-only import
// is erased before either sees it, so this module stays loadable by both.
import type { AgentRun } from "./agents";

/** How long a discovery answer is reused. Long enough that a busy panel is not shelling out
 *  constantly, short enough that opening a window shows up while you are still looking for it. */
export const DISCOVERY_TTL_MS = 20_000;

export interface Discovery {
  runs: AgentRun[];
  at: number;
}

/** Parse interact agents discovered — one JSON object per line.
 *
 *  A line that will not parse is dropped rather than failing the batch: the realistic cause is a
 *  warning on stdout from some tool in the chain, and losing one session beats losing all of them.
 */
export function parseDiscovered(stdout: string): AgentRun[] {
  const runs: AgentRun[] = [];
  for (const line of stdout.split("\n")) {
    if (!line.trim()) continue;
    try {
      const raw = JSON.parse(line);
      if (raw && typeof raw.run_id === "string") runs.push({ ...raw, foreign: true } as AgentRun);
    } catch {
      continue;
    }
  }
  return runs;
}

/** The on-disk runs plus the discovered ones, with disk winning any collision.
 *
 *  A session interact started AND a provider reports is the same session seen twice; the record
 *  is the better copy (it carries cost, tokens, the definition, the parent). Preferring the
 *  discovered one would replace a fully-described run with a stub and read as data loss.
 */
export function mergeDiscovered(
  onDisk: readonly AgentRun[],
  discovered: readonly AgentRun[],
): AgentRun[] {
  const known = new Set(onDisk.map((r) => r.run_id));
  const merged = [...onDisk, ...discovered.filter((r) => !known.has(r.run_id))];
  return merged.sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0));
}

/** Is a cached discovery still worth reusing? Separated so the staleness rule is testable without
 *  a clock or a subprocess. */
export function isFresh(cache: Discovery | null, now: number, ttl = DISCOVERY_TTL_MS): boolean {
  return cache !== null && now - cache.at < ttl;
}
