/** Typed usage identity/accounting that remains testable without VS Code or filesystem imports. */
export type { UsageEntry } from "./generated/types";
import type { UsageEntry } from "./generated/types";

export interface ProviderUsage {
  provider: string;
  cost: number;
  unknownCostCalls: number;
  sessionUsageCalls: number;
}

export function observedCost(entry: UsageEntry): number {
  return typeof entry.cost === "number" && Number.isFinite(entry.cost) ? entry.cost : 0;
}

export function aggregateUsageProviders(
  entries: UsageEntry[],
): ProviderUsage[] {
  const groups = new Map<string, ProviderUsage>();
  for (const entry of entries) {
    const provider = entry.provider || "unknown";
    const group = groups.get(provider) ?? {
      provider,
      cost: 0,
      unknownCostCalls: 0,
      sessionUsageCalls: 0,
    };
    group.cost += observedCost(entry);
    if (entry.cost == null) group.unknownCostCalls += 1;
    if (entry.billing === "session_usage") group.sessionUsageCalls += 1;
    groups.set(provider, group);
  }
  return [...groups.values()].sort((a, b) => b.cost - a.cost);
}

export function summarizeUsage(entries: UsageEntry[]): {
  observedMeteredApiCost: number;
  unknownAccountImpactCalls: number;
  unknownMeteredApiCostCalls: number;
  sessionUsageCalls: number;
} {
  return {
    observedMeteredApiCost: entries.reduce((sum, entry) => sum + observedCost(entry), 0),
    unknownAccountImpactCalls: entries.filter((entry) => entry.cost == null).length,
    unknownMeteredApiCostCalls: entries.filter(
      (entry) => entry.billing === "metered_api" && entry.cost == null,
    ).length,
    sessionUsageCalls: entries.filter((entry) => entry.billing === "session_usage").length,
  };
}
