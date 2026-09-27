/** The live model catalog, as the panel sees it.
 *
 *  The extension used to read ./models.json — a file baked into the bundle by sync-data.js at
 *  BUILD time. It looked current and wasn't: prices and context windows aged with every release
 *  and nothing said so. This reads the catalog Python refreshes (interact.model_catalog,
 *  ~/.interact/out/model_catalog.json) and, when that's missing or stale, fetches OpenRouter's
 *  public endpoint directly — a plain unauthenticated GET, no key.
 *
 *  The invariant, same as the Python side: the catalog carries its SOURCE and its AGE, and isLive
 *  goes false for anything stale. Serving old data offline is fine; serving it as today's truth
 *  is the bug.
 */
import * as fs from "fs";
import * as path from "path";

import { agentsDir } from "./paths";

/** Matches Python's TTL_SECONDS — the two write the same cache file, so they must agree on when
 *  it has gone off. */
import {
  type Catalog,
  type ModelInfo,
  TTL_SECONDS,
  ageSeconds,
} from "./catalogFormat";

export * from "./catalogFormat";


/** The file Python writes. It sits beside the agent registry on the same fixed path, so a process
 *  that never saw INTERACT_DEBUG_DIR still finds it. */
export function catalogPath(): string {
  return path.join(path.dirname(agentsDir()), "model_catalog.json");
}

export function readCatalog(): Catalog | null {
  try {
    const raw = JSON.parse(fs.readFileSync(catalogPath(), "utf8"));
    if (!Array.isArray(raw?.models) || raw.models.length === 0) return null;
    return {
      models: raw.models.filter((m: ModelInfo) => m && typeof m.id === "string"),
      source: String(raw.source || "openrouter"),
      fetched_at: Number(raw.fetched_at || 0),
    };
  } catch {
    return null; // absent or truncated — the caller asks the CLI to rebuild it
  }
}

/** The freshest catalog available, never throwing.
 *
 *  A cache inside its TTL wins (no network on every panel refresh); otherwise fetch; otherwise
 *  serve the stale cache, which isLive already marks as stale. Returns null only when there's
 *  genuinely nothing — the caller then says so rather than rendering an empty panel.
 */
export async function loadCatalog(): Promise<Catalog | null> {
  const cached = readCatalog();
  if (cached && ageSeconds(cached) <= TTL_SECONDS) return cached;

  // Ask PYTHON to refresh rather than fetching here. This used to fetch OpenRouter and write the
  // cache itself, with a narrower schema (no output_cost_per_token) — so whichever side wrote
  // last decided whether output prices existed at all. One writer, one schema.
  await refreshViaCli();
  return readCatalog() ?? cached;
}

/** Run interact refresh, best-effort: a panel must render whatever it has even with no CLI. */
function refreshViaCli(): Promise<void> {
  return new Promise((resolve) => {
    import("child_process")
      .then(({ execFile }) => {
        const child = execFile("interact", ["refresh"], { timeout: 20000 }, () => resolve());
        child.on("error", () => resolve());
      })
      .catch(() => resolve());
  });
}
