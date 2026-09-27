/** Where interact's local output lives — the ONE place the extension resolves it.
 *
 *  Deliberately dependency-free (node builtins only, no vscode): the extension host and the
 *  Python parity test (tests/test_paths.py) both load this, and pulling in vscode would break
 *  the latter. Mirrors Python's Config.debug_dir / Config.usage_log
 *  (src/interact/config/settings.py) — the two MUST agree, or the dashboard charts a file
 *  nothing writes.
 */
import * as os from "os";
import * as path from "path";

/** interact's config file — the CLI, the MCP server and this extension all read/write this ONE
 *  file (it is the live source of truth for keys + settings), so its location lives here rather
 *  than being re-derived from homedir() at each use. */
export const INTERACT_CONFIG_PATH = path.join(os.homedir(), ".interact", "config.env");

/** Default base dir, mirroring Python's Config.debug_dir (~/.interact/out, not ~/.interact
 *  — output lives in out/ so the root stays just config.env + out/). */
export const DEFAULT_DEBUG_DIR = "~/.interact/out";

/** Expand a leading ~ — VS Code settings are free text, so users type ~/.... */
export function expandHome(p: string): string {
  return p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

/** The usage log under a given interact base dir (mirrors Python's Config.usage_log).
 *  NOTE: <baseDir>/usage.jsonl, with NO logs/ segment — 8fb56b1 moved the Python writer
 *  out of <debug_dir>/logs/ and this reader was left behind, so the panel charted a file
 *  nothing writes. tests/test_paths.py binds the two. */
export function usageLogPathFor(baseDir: string): string {
  return path.join(expandHome(baseDir || DEFAULT_DEBUG_DIR), "usage.jsonl");
}

/** The agent-run registry directory.
 *
 *  DELIBERATELY NOT debug_dir-relative, unlike the usage log above. This is cross-process IPC —
 *  the CLI writes records, this extension reads them, another shell stops a run — so every
 *  participant must agree on one location regardless of whether it saw INTERACT_DEBUG_DIR.
 *  Disposable test and capture launches set INTERACT_AGENTS_DIR for all participants.
 *  A debug-dir-relative registry would reproduce, at the feature level, exactly the bug 0ef5fa4
 *  fixed for metering. Mirrors Python's interact.agents.registry.agents_dir(); tests/test_paths.py
 *  binds the two.
 */
export function agentsDir(): string {
  if (process.env.INTERACT_AGENTS_DIR) return expandHome(process.env.INTERACT_AGENTS_DIR);
  return path.join(os.homedir(), ".interact", "out", "agents");
}
