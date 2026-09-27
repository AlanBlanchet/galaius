/** The live leaderboard, read straight off disk by the panel.
 *
 *  Scores used to arrive pre-joined from interact agents models --json-out, which made every
 *  number in this product depend on the VERSION of whichever interact happens to be on PATH. On
 *  a machine where that binary came from a different checkout than the panel, the join simply
 *  didn't happen: 436 models, 0 scored, three different sentences asserting "no ranking carries
 *  this model" — all false against the Benchmarks tab in the same window, reading this same file.
 *
 *  So the panel joins for itself. ~/.interact/out/benchmark_scores.json is a fixed path both
 *  front ends already read (ttl_cache.py says so in as many words), and leaderboardKey is the
 *  same normalizer the webview already applies to its own rows.
 */
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { bareModelName, leaderboardKey } from "./competence.ts";

export function boardPath(): string {
  return path.join(os.homedir(), ".interact", "out", "benchmark_scores.json");
}

interface BoardRow { name?: unknown; intelligence?: unknown }

/** Every model the board measures, keyed by leaderboardKey, best score per key.
 *
 *  Never throws, never partially fails: a missing, unreadable or reshaped file is an EMPTY
 *  board, which callers must render as "not known here", never "not scored".
 */
export function boardScores(file: string = boardPath()): Map<string, number> {
  const best = new Map<string, number>();
  let rows: BoardRow[];
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf8")) as { scores?: BoardRow[] };
    rows = Array.isArray(raw?.scores) ? raw.scores : [];
  } catch { return best; }
  for (const row of rows) {
    const name = typeof row?.name === "string" ? row.name.trim() : "";
    const score = typeof row?.intelligence === "number" ? row.intelligence : null;
    if (!name || score === null || !Number.isFinite(score)) continue;
    // ONE normalizer on BOTH sides of the join, same rule as live_scores in Python: leaderboardKey
    // turns a display name into a slug, bareModelName puts that slug in the same space as a model
    // id — what every caller looks up with. Keying by the slug alone meant the two never met over
    // a maturity word ("Gemini 3.1 Pro Preview" indexed as gemini-3-1-pro-preview, sought as
    // gemini-3-1-pro).
    //
    // Also collapses one model's several effort settings ("Max Effort", "Low Effort") onto one
    // key; the best of them is that model's number.
    const key = bareModelName(leaderboardKey(name));
    best.set(key, Math.max(best.get(key) ?? Number.NEGATIVE_INFINITY, score));
  }
  return best;
}

/** What the board says about one model, and how sure that is.
 *
 *  approximate is never cosmetic: surfaces render it as ≈ and name what was matched, because
 *  asserting a measured score for a model nobody measured is worse than saying nothing.
 */
export interface BoardHit { key: string; score: number; approximate: boolean }

const parts = (key: string): string[] => key.split("-").filter(Boolean);

/** Find a model on the board, exactly if possible and honestly otherwise.
 *
 *  Four routes, each weaker than the last, because a fleet records models under names no board
 *  ever prints: a run says sonnet, board says Claude Sonnet 5; catalog says claude-haiku-4-5,
 *  board says Claude 4.5 Haiku. Refusing all of those left thirteen roster rows asserting "no
 *  ranking carries this model" about models the board ranks.
 */
export function boardMatch(
  modelId: string, board: Map<string, number>, bare: (id: string) => string,
): BoardHit | undefined {
  for (const key of [bare(modelId), bareModelName(leaderboardKey(modelId))]) {
    const score = board.get(key);
    if (score !== undefined) return { key, score, approximate: false };
  }
  const wanted = parts(bare(modelId));

  // Same words, different order — claude-haiku-4-5 and claude-4-5-haiku are one model.
  const sorted = [...wanted].sort().join("-");
  for (const [key, score] of board) {
    if (parts(key).sort().join("-") === sorted) return { key, score, approximate: true };
  }

  // A TIER (sonnet, opus) names a FAMILY, not a model — vendor points it at the best of that
  // family; same for a name carrying an extra qualifier the board never printed
  // (gpt-5-1-codex-max where the board stops at gpt-5-1-codex).
  let best: BoardHit | undefined;
  for (const [key, score] of board) {
    const theirs = parts(key);
    const shortName = wanted.length <= 2 && wanted.every((word) => theirs.includes(word));
    const prefix = wanted.length > 2 && theirs.length <= wanted.length
      && theirs.every((word, i) => wanted[i] === word);
    if (!shortName && !prefix) continue;
    if (!best || score > best.score) best = { key, score, approximate: true };
  }
  return best;
}

/** Just the number, for callers that only rank. */
export function boardScoreOf(
  modelId: string, board: Map<string, number>, bare: (id: string) => string,
): number | undefined {
  return boardMatch(modelId, board, bare)?.score;
}
