/** Live per-benchmark leaderboard tables, preferred over the bundled snapshot.
 *
 *  benchmarks.json is baked into the extension at BUILD time, so its scores can never change
 *  once packaged — which is how a months-old model stayed on screen as the best at MMMU. Python
 *  fetches the upstream leaderboards at runtime into ~/.interact/out/benchmark_tables.json;
 *  this reads that and lets it win, per benchmark, falling back to the snapshot for any the
 *  upstreams could not answer.
 */
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export interface LiveTable {
  source_url?: string;
  retrieved?: string;
  freshness?: "current" | "stale" | "unknown";
  entries: {
    model_name: string;
    model_id?: string | null;
    score: number;
    normalized_score?: number | null;
    status?: "eligible" | "unverified" | "missing" | "not_applicable" | "approximate" | "unmapped";
  }[];
}

export function tablesPath(): string {
  // The same fixed location Python writes, and the one leaderboard.ts already reads from —
  // deliberately not following INTERACT_DEBUG_DIR, since two front ends must find one file.
  return path.join(os.homedir(), ".interact", "out", "benchmark_tables.json");
}

/** What Python cached, keyed by benchmark id. Empty when absent — never throws at a panel. */
export function readLiveTables(): Record<string, LiveTable> {
  try {
    const raw = JSON.parse(fs.readFileSync(tablesPath(), "utf8"));
    return (raw?.tables as Record<string, LiveTable>) ?? {};
  } catch {
    return {};
  }
}

/** The bundled file with live tables merged in. Returns a COPY: the bundle is required once and
 *  shared, so mutating it would leak one render's data into every later one. */
export function mergeLiveTables(bundled: any, live: Record<string, LiveTable>): any {
  // An empty live table is not an answer — an upstream can return 200 with zero rows, and
  // letting that win would blank a row that at least had an honest, dated snapshot in it.
  const usable = Object.entries(live).filter(([id, t]) =>
    t?.entries?.length && !(
      id === "mmmu_pro" && t.source_url === "https://mmmu-benchmark.github.io/"
      || id === "video_mme" && t.source_url === "https://video-mme.github.io/"
    ),
  );
  if (!usable.length) return bundled;
  const byId = Object.fromEntries(usable);
  return {
    ...bundled,
    benchmarks: (bundled?.benchmarks ?? []).map((b: any) =>
      byId[b.id] && isFresher(byId[b.id].retrieved, b.published?.retrieved)
        ? { ...b, published: { ...b.published, ...byId[b.id] } }
        : b,
    ),
  };
}

/** Whether a fetched table is at least as recent as the one it would replace.
 *
 *  "Live" is not the same as "current": OpenVLM's MMMU board last published in 2025, older than
 *  the packaged snapshot, so taking it merely because it came off the network would have made the
 *  panel worse. The freshest DATED source wins, whichever side it is on.
 */
function isFresher(fetched: string | undefined, bundled: string | undefined): boolean {
  if (!bundled) return true;
  if (!fetched) return false;
  return fetched >= bundled; // ISO dates compare correctly as strings
}

/** Shown ON the row. A retrieved date hidden in a tooltip reads as current, which is the whole
 *  defect: the number looks like today's truth until you hover it. */
export function provenanceLabel(retrieved: string | undefined): string {
  return retrieved ? `as of ${retrieved}` : "date unknown";
}

export function selectionExplanation(status: string): string {
  const labels: Record<string, string> = {
    eligible: "Eligible for routing.",
    unverified: "Unverified benchmark value; visible for context but excluded from routing.",
    missing: "Missing benchmark value; excluded, never imputed as zero.",
    not_applicable: "Not applicable to this model capability; excluded.",
    approximate: "Approximate benchmark value; excluded from routing.",
    stale: "Stale benchmark value; excluded until refreshed.",
    unmapped: "Unmapped model identity; excluded to prevent a wrong-provider match.",
  };
  return labels[status] ?? "Unsupported benchmark status; excluded.";
}

export function trustedBenchmarkSource(url: string): string | undefined {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}
