/** Token counts as the runtime writes them (`TokenCounts`: additive — `input` the uncached prompt, the cache reads and
 *  writes beside it). A figure nothing reported is absent, never a synthetic zero. */
import type { TokenCounts } from "../generated/types";

type Figure = "input" | "output" | "cache_read" | "cache_write" | "reasoning";
const FIGURES: readonly Figure[] = ["input", "output", "cache_read", "cache_write", "reasoning"];

function byFigure(a: TokenCounts, b: TokenCounts, join: (x: number, y: number) => number): TokenCounts {
  return Object.fromEntries(FIGURES.map((name) => {
    const [x, y] = [a[name], b[name]];
    return [name, x == null && y == null ? null : join(x ?? 0, y ?? 0)];
  })) as TokenCounts;
}

/** Two counts as one (`TokenCounts.__add__`). */
export function addCounts(a: TokenCounts, b: TokenCounts): TokenCounts {
  return byFigure(a, b, (x, y) => x + y);
}

/** The newer of two readings of one cumulative count, figure by figure; undefined when neither counted any. */
export function latestCounts(a: TokenCounts | null | undefined, b: TokenCounts | null | undefined): TokenCounts | undefined {
  return a == null ? b ?? undefined : b == null ? a : byFigure(a, b, Math.max);
}

/** The whole prompt a model saw (uncached + cache reads + cache writes); null when nothing was counted. */
export function promptTokens(usage: TokenCounts | null | undefined): number | null {
  if (usage == null || (usage.input == null && usage.cache_read == null && usage.cache_write == null)) return null;
  return (usage.input ?? 0) + (usage.cache_read ?? 0) + (usage.cache_write ?? 0);
}

/** The token fields an older runtime wrote on a run record, an event line or a media usage-log row: the WHOLE prompt as
 *  `input_tokens`, its cache reads and writes inside it (Python `galaius.usage_dialects.OlderTokens`, the same edge). */
const OLDER_FIELDS = ["input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens"] as const;

/** What an older runtime wrote on this PC, read in the one spelling: its whole-prompt fields as its `usage` (the uncached
 *  part carved out, never counted twice), an event's rate-limit windows (once under `usage`) as its `windows`. A row
 *  already in the one spelling passes unchanged. The files only grow or wait for a rewrite: those rows stay. */
export function olderWritten<Row extends Record<string, unknown>>(row: Row): Row {
  const { usage, ...rest } = row as Record<string, unknown>;
  const read: Record<string, unknown> = Array.isArray(usage) ? { ...rest, windows: usage } : usage === undefined ? rest : { ...rest, usage };
  if (!OLDER_FIELDS.some((name) => name in read)) return read as Row;
  const [prompt, output, cacheRead, cacheWrite] = OLDER_FIELDS.map((name) => (typeof read[name] === "number" ? read[name] as number : null));
  for (const name of OLDER_FIELDS) delete read[name];
  if (read.usage === undefined && [prompt, output, cacheRead, cacheWrite].some((figure) => figure !== null)) {
    const input = prompt === null ? null : Math.max(prompt - (cacheRead ?? 0) - (cacheWrite ?? 0), 0);
    read.usage = Object.fromEntries(Object.entries({ input, output, cache_read: cacheRead, cache_write: cacheWrite }).filter(([, figure]) => figure !== null));
  }
  return read as Row;
}
