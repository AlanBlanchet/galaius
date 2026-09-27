/** The live Artificial Analysis leaderboard, as the panel sees it.
 *
 *  The bundled benchmarks.json says WHICH benchmarks matter; it cannot carry SCORES, because
 *  AA's free tier is "internal use only, no redistribution" — so scores are fetched at runtime
 *  with the user's own key and cached under their home dir by interact.benchmark_source.
 *
 *  Same invariant as the model catalog: the board carries its SOURCE and AGE, and isLive goes
 *  false once stale. Serving old numbers offline is fine; serving them as today's truth is not.
 */
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/** Matches Python's TTL_SECONDS — both read the same cache, so they must agree on staleness. */
const TTL_SECONDS = 12 * 60 * 60;

export interface Score {
  name: string;
  creator: string;
  intelligence: number;
}

export interface Leaderboard {
  scores: Score[];
  source: string;
  ageSeconds: number;
  isLive: boolean;
}

export function leaderboardPath(): string {
  return path.join(os.homedir(), ".interact", "out", "benchmark_scores.json");
}

export function describeAge(seconds: number): string {
  if (!isFinite(seconds)) return "never fetched";
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

/** null when there is nothing cached — the caller then says so rather than rendering an empty
 *  table, which would read as "no good models" instead of "no data". */
export function readLeaderboard(): Leaderboard | null {
  let raw: { source?: string; fetched_at?: number; scores?: Score[] };
  try {
    raw = JSON.parse(fs.readFileSync(leaderboardPath(), "utf8"));
  } catch {
    return null;
  }
  const scores = (raw.scores ?? []).filter(
    (s) => s && typeof s.name === "string" && typeof s.intelligence === "number",
  );
  if (!scores.length) return null;
  const ageSeconds = raw.fetched_at ? Math.max(0, Date.now() / 1000 - raw.fetched_at) : Infinity;
  return {
    scores,
    source: raw.source ?? "artificial_analysis",
    ageSeconds,
    isLive: raw.source === "artificial_analysis" && ageSeconds <= TTL_SECONDS,
  };
}
