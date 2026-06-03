import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { ModelsData, providerOf, RangeId } from "./shared";

export interface UsageEntry {
  timestamp: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cost: number;
}

export interface DailyCost {
  date: string;
  cost: number;
}

const LOG_DIR = path.join(os.homedir(), ".interact", "logs");
const LOG_FILE = path.join(LOG_DIR, "usage.jsonl");

// Theme-only chart palette — rotates by hash(name).
export const CHART_COLORS = [
  "var(--vscode-charts-blue)",
  "var(--vscode-charts-green)",
  "var(--vscode-charts-orange)",
  "var(--vscode-charts-purple)",
  "var(--vscode-charts-yellow)",
  "var(--vscode-charts-red)",
];

export function colorFor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return CHART_COLORS[h % CHART_COLORS.length];
}

export function ensureLogDir(): void {
  fs.mkdirSync(LOG_DIR, { recursive: true });
}

export async function readUsageLog(): Promise<UsageEntry[]> {
  try {
    const content = await fs.promises.readFile(LOG_FILE, "utf8");
    const entries: UsageEntry[] = [];
    for (const line of content.split("\n")) {
      if (!line.trim()) continue;
      try {
        entries.push(JSON.parse(line));
      } catch {}
    }
    return entries;
  } catch {
    return [];
  }
}

export function aggregateDailyCost(entries: UsageEntry[]): DailyCost[] {
  const byDate = new Map<string, number>();
  for (const e of entries) {
    const date = e.timestamp.slice(0, 10);
    byDate.set(date, (byDate.get(date) ?? 0) + e.cost);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, cost]) => ({ date, cost }));
}

const RANGE_HOURS: Record<RangeId, number | null> = {
  "24h": 24,
  "7d": 24 * 7,
  "30d": 24 * 30,
  all: null,
};

export function filterByRange(
  entries: UsageEntry[],
  range: RangeId,
): UsageEntry[] {
  const hours = RANGE_HOURS[range];
  if (hours == null) return entries;
  const cutoff = Date.now() - hours * 3600 * 1000;
  return entries.filter((e) => {
    const t = Date.parse(e.timestamp);
    return Number.isFinite(t) && t >= cutoff;
  });
}

export function aggregateByProvider(
  entries: UsageEntry[],
  modelsData: ModelsData,
): { provider: string; cost: number }[] {
  const byProv = new Map<string, number>();
  for (const e of entries) {
    const prov = providerOf(e.model, modelsData) ?? "unknown";
    byProv.set(prov, (byProv.get(prov) ?? 0) + e.cost);
  }
  return [...byProv.entries()]
    .map(([provider, cost]) => ({ provider, cost }))
    .sort((a, b) => b.cost - a.cost);
}

function dayKey(ts: string): string {
  return ts.slice(0, 10);
}

function buildDayBuckets(days: number): string[] {
  const out: string[] = [];
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getTime() - i * 86400000);
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

export function aggregateStackedByModel(
  entries: UsageEntry[],
  days: number,
  topN = 5,
): {
  xLabels: string[];
  series: { name: string; color: string; values: number[] }[];
} {
  const xLabels = buildDayBuckets(days);
  const dayIdx = new Map(xLabels.map((d, i) => [d, i]));

  const totalByModel = new Map<string, number>();
  for (const e of entries) {
    totalByModel.set(e.model, (totalByModel.get(e.model) ?? 0) + e.cost);
  }
  const ranked = [...totalByModel.entries()].sort((a, b) => b[1] - a[1]);
  const top = new Set(ranked.slice(0, topN).map(([m]) => m));
  const hasOther = ranked.length > topN;

  const seriesMap = new Map<string, number[]>();
  const initRow = () => Array<number>(xLabels.length).fill(0);
  for (const m of top) seriesMap.set(m, initRow());
  if (hasOther) seriesMap.set("other", initRow());

  for (const e of entries) {
    const idx = dayIdx.get(dayKey(e.timestamp));
    if (idx == null) continue;
    const key = top.has(e.model) ? e.model : "other";
    const row = seriesMap.get(key);
    if (!row) continue;
    row[idx] += e.cost;
  }

  const series = [...seriesMap.entries()].map(([name, values]) => ({
    name,
    color:
      name === "other"
        ? "var(--vscode-descriptionForeground)"
        : colorFor(name),
    values,
  }));
  return { xLabels: xLabels.map((d) => d.slice(5)), series };
}

export function aggregateTokensByModel(
  entries: UsageEntry[],
): { model: string; inputTokens: number; outputTokens: number }[] {
  const byModel = new Map<string, { input: number; output: number }>();
  for (const e of entries) {
    const cur = byModel.get(e.model) ?? { input: 0, output: 0 };
    cur.input += e.input_tokens;
    cur.output += e.output_tokens;
    byModel.set(e.model, cur);
  }
  return [...byModel.entries()]
    .map(([model, v]) => ({
      model,
      inputTokens: v.input,
      outputTokens: v.output,
    }))
    .sort(
      (a, b) =>
        b.inputTokens + b.outputTokens - (a.inputTokens + a.outputTokens),
    );
}

export function aggregateCallsByModel(
  entries: UsageEntry[],
): { model: string; calls: number }[] {
  const byModel = new Map<string, number>();
  for (const e of entries) {
    byModel.set(e.model, (byModel.get(e.model) ?? 0) + 1);
  }
  return [...byModel.entries()]
    .map(([model, calls]) => ({ model, calls }))
    .sort((a, b) => b.calls - a.calls);
}

