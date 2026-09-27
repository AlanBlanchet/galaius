import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import { INTERACT_CONFIG_PATH } from "./paths";

export const SETTING_SECTION = "interact";
export const IS_SECRET_RE = /KEY|SECRET|TOKEN/i;

// Canonical types — generated from src/interact/api_types.py.
// Run clients/vscode/scripts/generate-types.sh to refresh.
export type {
  Model,
  ModelSpec,
  ProviderSpec,
  ModelsConfig,
  Benchmark,
  BenchmarkRecommendation,
  CoordFormat,
  PublishedEntry,
  PublishedTable,
} from "./generated/types";

import type {
  AgentRun as GeneratedAgentRun,
  ModelSpec,
  ProviderSpec,
  ModelsConfig,
} from "./generated/types";
import type { BillingPresentation, ChargePath, CostCertainty } from "./billingPresentation";

export interface Action {
  type: string;
  label: string;
  data?: Record<string, string>;
  style?: "primary" | "secondary";
}

export type RangeId = "24h" | "7d" | "30d" | "all";

/** How the agent board splits its lanes. The owner watches a team three ways: whose project it is,
 *  which vendor is burning the quota, which model is doing the work. */
export type AgentGroupBy = "project" | "provider" | "model" | "none";

/** Generated persisted vocabulary plus the one roster-only status synthesized by the Team UI. */
export type AgentStatus = NonNullable<GeneratedAgentRun["status"]> | "declared";

/** One agent as the board draws it: an identity plus an interval on a shared clock.
 *
 *  endedAt is null for a run with no recorded end — which happens for a crash the registry
 *  never got to stamp. That's NOT the same as "ended now": the board draws an explicit
 *  unknown-end tail rather than inventing a duration, because a fabricated bar length would be
 *  a lie told in pixels.
 */
export interface AgentLane {
  id: string;
  name: string;
  provider: string;
  model?: string;
  /** What this model is measured at, spelled out — "aa.intelligence 38.4 · 24th of 450 scored".
   *  "Models should also spell out their intelligence score, such that we can compare the most
   *  competents and trust one agent more than another in some situations (isn't absolute)". */
  competence?: string;
  /** The model this run's recorded name DENOTES. A run records sonnet where the board prints
   *  Claude Sonnet 5; grouping on the raw name split one model into two groups, each with its
   *  own run count and spend and the same score. */
  resolvedModel?: string;
  /** The measure as a NUMBER. Ordering read it out of the competence SENTENCE once, which for an
   *  approximate row begins with the matched model's own NAME — and sorted 50.7 below 30.9. */
  score?: number;
  /** Display name of the run's cwd (its basename) — the "project" grouping key. */
  project?: string;
  /** Full cwd, for the tooltip. */
  cwd?: string;
  task?: string;
  status: AgentStatus;
  parentId?: string | null;
  /** Ancestry depth within the whole run set, for the tree rail. */
  depth: number;
  /** Epoch milliseconds. */
  startedAt: number;
  /** Epoch milliseconds; null = still open (running) or never recorded (see above). */
  endedAt?: number | null;
  /** Reported or estimated USD value. Its meaning is defined by chargePath, never by this number. */
  costUsd: number | null;
  chargePath: ChargePath;
  costCertainty: CostCertainty;
  last?: string;
}

export type CellContent =
  | {
      kind: "row";
      label: string;
      value?: string;
      dot?: "ok" | "missing";
      actions?: Action[];
      tooltip?: string;
    }
  | {
      kind: "setting";
      key: string;
      label: string;
      description: string;
      input: "bool" | "enum" | "int" | "str" | "path";
      value: string;
      defaultValue: string;
      minimum?: number;
      pattern?: string;
      options?: { label: string; value: string }[];
    }
  | { kind: "prompt-workspace"; files: string[]; selected?: string; content?: string;
      digest?: string; status?: string; revision?: number }
  | { kind: "table"; headers: string[]; rows: string[][] }
  | { kind: "chart"; points: { x: string; y: number }[]; yPrefix?: string }
  | {
      kind: "bar-h";
      bars: { label: string; value: number; color?: string }[];
      valuePrefix?: string;
      ariaSummary: string;
    }
  | {
      kind: "stacked-bar";
      xLabels: string[];
      series: { name: string; color: string; values: number[] }[];
      valuePrefix?: string;
      ariaSummary: string;
    }
  | {
      kind: "small-multiples";
      panels: {
        title: string;
        bars: { label: string; value: number; color: string }[];
      }[];
      valuePrefix?: string;
      ariaSummary: string;
    }
  | {
      kind: "donut";
      segments: { label: string; value: number; color: string }[];
      centerLabel?: string;
      ariaSummary: string;
    }
  | {
      kind: "range-selector";
      current: RangeId;
      options: { id: RangeId; label: string }[];
    }
  | {
      /** The agent supervision board: lanes on a shared time axis, grouped and totalled.
       *  Grouping and per-group totals are derived in the webview from lanes + groupBy, so the
       *  host ships facts and the renderer owns presentation — one place to change either. */
      kind: "agent-board";
      groupBy: AgentGroupBy;
      lanes: AgentLane[];
      /** Epoch ms bounds of the shared clock every lane and the ribbon are drawn against. */
      windowStart: number;
      now: number;
      billing: BillingPresentation;
      ariaSummary: string;
    }
  | { kind: "heading"; text: string }
  | { kind: "empty"; message: string; hint?: string };

export interface CellUpdate {
  id: string;
  title: string;
  content: CellContent[];
}

// UI-side enrichment of ModelSpec — generate-models.py injects fields
// (intelligence_score, coord_format) that the server schema doesn't carry.
export type ModelInfo = ModelSpec & {
  coord_format?: string;
  intelligence_score?: number;
};

// ProviderSpec's envKeys/models are optional in the generated schema
// (Pydantic defaults). At runtime the generated models.json always
// populates them, so the UI assumes presence.
export type ProviderInfo = ProviderSpec & {
  envKeys: string[];
  models: Record<string, ModelInfo>;
};

// generate-models.py emits extra UI-only top-level keys not present in
// the server-side ModelsConfig schema.
export type ModelsData = ModelsConfig & {
  providers: Record<string, ProviderInfo>;
  taskDescriptions?: Record<string, string>;
  keyAliases?: Record<string, string>;
};

/** One user setting — mirrors interact.config.schema.Setting (bundled as settings.json). */
export interface Setting {
  key: string;
  field: string;
  label: string;
  description: string;
  group: string;
  kind: "model" | "enum" | "bool" | "int" | "str" | "path";
  role: string | null;
  options: { label: string; value: string }[] | null;
  env: string;
  default: string;
  minimum: number | null;
  pattern: string | null;
}

// The shared settings schema — the SAME spec the interact TUI renders. Bundled from the Python
// package (src/interact/data/settings.json, regenerated by
// python -m interact.config.schema), so the extension's env mapping and config panel can't
// drift from the CLI's.
import settingsData from "./settings.json";
export const SETTINGS: Setting[] = (settingsData as { settings: Setting[] }).settings;

/** Friendly setting key → the INTERACT_* env var the server reads (derived from the schema). */
export const SETTING_ENV_MAP: Record<string, string> = Object.fromEntries(
  SETTINGS.map((s) => [s.key, s.env]),
);

/** Model-role settings → the role name, for resolving the per-task model. */
export const SETTING_TO_TASK: Record<string, string> = Object.fromEntries(
  SETTINGS.filter((s) => s.kind === "model").map((s) => [s.key, s.role as string]),
);

/** Path of interact's shared config store (the same file the CLI/TUI use). Defined in paths.ts —
 *  the one module that owns where interact's local files live — and re-exported here for the
 *  existing importers. */
export { INTERACT_CONFIG_PATH };

/** API-key store backed by interact's own ~/.interact/config.env — NOT VS Code secret
 *  storage. This makes interact the single source of truth for keys: the same file the
 *  interact CLI/TUI manage and that the interact mcp server reads at startup, so a key set
 *  in any of them is seen everywhere. Nothing here touches VS Code SecretStorage.
 */
export class KeyManager {
  private cache = new Map<string, string>();


  private static readFile(): Map<string, string> {
    const map = new Map<string, string>();
    try {
      for (const raw of fs.readFileSync(INTERACT_CONFIG_PATH, "utf8").split("\n")) {
        const line = raw.trim();
        if (!line || line.startsWith("#")) continue;
        const eq = line.indexOf("=");
        if (eq > 0) map.set(line.slice(0, eq).trim(), line.slice(eq + 1).trim());
      }
    } catch {
      /* missing/unreadable file → empty */
    }
    return map;
  }

  private static writeFile(map: Map<string, string>): void {
    fs.mkdirSync(path.dirname(INTERACT_CONFIG_PATH), { recursive: true });
    const body = [...map.entries()].map(([k, v]) => `${k}=${v}`).join("\n");
    fs.writeFileSync(INTERACT_CONFIG_PATH, body + "\n", { mode: 0o600 });
  }

  async loadAll(allEnvKeys: string[]): Promise<void> {
    const file = KeyManager.readFile();
    this.cache = new Map();
    for (const key of allEnvKeys) {
      const val = file.get(key);
      if (val) this.cache.set(key, val);
    }
  }

  get(key: string): string | undefined {
    return this.cache.get(key);
  }

  async set(key: string, value: string): Promise<void> {
    const file = KeyManager.readFile();
    file.set(key, value);
    KeyManager.writeFile(file);
    this.cache.set(key, value);
  }

  async remove(key: string): Promise<void> {
    const file = KeyManager.readFile();
    file.delete(key);
    KeyManager.writeFile(file);
    this.cache.delete(key);
  }

  entries(): [string, string][] {
    return [...this.cache.entries()];
  }

  missingKeys(required: string[]): string[] {
    return required.filter((k) => !this.cache.has(k));
  }
}

export function formatLabel(key: string): string {
  return key
    .split(".")
    .map((s) => s.replace(/^./, (c) => c.toUpperCase()))
    .join(" ");
}

export function cfg() {
  return vscode.workspace.getConfiguration(SETTING_SECTION);
}

export function providerOf(
  model: string,
  modelsData: ModelsData,
): string | undefined {
  for (const [provider, info] of Object.entries(modelsData.providers)) {
    if (model in info.models) return provider;
  }
}

export function metaOf(model: string, data: ModelsData): ModelInfo | undefined {
  const prov = providerOf(model, data);
  return prov ? data.providers[prov]?.models[model] : undefined;
}

/** The extension's own version — used to pin the uvx install to the matching released git tag,
 *  so a colleague gets exactly the interact that ships with their extension, never main/dev
 *  HEAD. */
function extensionVersion(): string {
  try {
    const pkg = path.join(__dirname, "..", "package.json");
    return JSON.parse(fs.readFileSync(pkg, "utf8")).version || "";
  } catch {
    return "";
  }
}

/** A local interact checkout to run instead of the released build — **DEV ONLY, opt-in**. Set the
 *  interact.projectPath setting, or the INTERACT_PROJECT_PATH env var (e.g. exported from a
 *  .env you source). It is **never** auto-detected from the workspace: a colleague who merely
 *  opens a clone of this repo must still get the released build, not your working tree. */
export function devProjectPath(): string {
  return (cfg().get<string>("projectPath") || process.env.INTERACT_PROJECT_PATH || "").trim();
}

/** Resolve the command to launch interact.
 *  Shared between MCP server registration and dashboard subprocess spawning.
 */
export function resolveCommand(log?: vscode.OutputChannel): [string, string[]] {
  const dev = devProjectPath();
  if (dev) {
    log?.appendLine(`Dev mode (opt-in): running interact from ${dev}`);
    return ["uv", ["run", "--directory", dev, "interact", "mcp"]];
  }
  // Default for everyone: the published build via uvx (interact isn't on PyPI — the bare
  // interact name belongs to an unrelated package there), pinned to this extension's release
  // tag so the server matches the extension. uvx caches the build after the first run.
  const v = extensionVersion();
  const ref = v ? `@v${v}` : "";
  log?.appendLine(`Running published interact via uvx (${ref || "main"})`);
  return ["uvx", ["--from", `git+https://github.com/AlanBlanchet/interact${ref}`, "interact", "mcp"]];
}

/** A local interact checkout, if one is opted into (same source as resolveCommand). DEV ONLY. */
export function resolveProjectPath(): string | undefined {
  return devProjectPath() || undefined;
}

export async function ensureKeys(
  provider: string,
  modelsData: ModelsData,
  keyManager: KeyManager,
  emitter: vscode.EventEmitter<void>,
): Promise<boolean> {
  const info = modelsData.providers[provider];
  if (!info) return true;
  const missing = keyManager.missingKeys(info.envKeys);
  for (const key of missing) {
    if (process.env[key]) {
      const use = await vscode.window.showInformationMessage(
        `Found ${key} in your environment. Use it?`,
        "Yes",
        "No",
      );
      if (use === "Yes") {
        await keyManager.set(key, process.env[key]!);
        emitter.fire();
        continue;
      }
    }
    const value = await vscode.window.showInputBox({
      prompt: `Enter your ${key}`,
      password: IS_SECRET_RE.test(key),
      ignoreFocusOut: true,
    });
    if (!value) return false;
    await keyManager.set(key, value);
    emitter.fire();
  }
  return true;
}
