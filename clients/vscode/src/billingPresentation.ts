/** Honest billing copy derived from persisted run facts.
 *
 * This module owns the words used by both agent surfaces. A numeric API-equivalent value is not
 * enough to infer whether the provider billed an API, consumed subscription quota or credits, or
 * ran on local hardware, so every amount stays attached to its charge path and certainty.
 */
import type { AgentRun } from "./generated/types";

export type ChargePath = NonNullable<AgentRun["charge_path"]>;
export type CostCertainty = NonNullable<AgentRun["cost_certainty"]>;

export interface BillingFact {
  chargePath: ChargePath;
  costCertainty: CostCertainty;
  costUsd: number | null;
}

export interface BillingLine {
  chargePath: ChargePath;
  count: number;
  text: string;
}

export interface BillingPresentation {
  heading: "Billing path" | "Mixed billing paths";
  lines: BillingLine[];
  ariaSummary: string;
}

function reportedCost(cost: number | null): number | null {
  return cost != null && Number.isFinite(cost) && cost >= 0 ? cost : null;
}

function usd(cost: number, estimated: boolean): string {
  return `${estimated ? "~" : ""}$${cost >= 100 ? cost.toFixed(2) : cost.toFixed(4)}`;
}

function describePath(chargePath: ChargePath, facts: readonly BillingFact[]): string {
  const reported = facts.map((fact) => reportedCost(fact.costUsd));
  const amounts = reported.filter((cost): cost is number => cost != null);
  const total = amounts.reduce((sum, cost) => sum + cost, 0);
  const complete = amounts.length === facts.length;
  const certain = complete && facts.every((fact) => fact.costCertainty === "known");

  if (chargePath === "metered_api") {
    if (amounts.length === 0) return "Metered API · provider charge not reported";
    return certain
      ? `${usd(total, false)} observed metered API charge`
      : `${usd(total, true)} metered API estimate · final provider charge not reported`;
  }
  if (chargePath === "subscription_quota") {
    const amount = amounts.length ? `${usd(total, true)} API-equivalent usage · ` : "";
    return `${amount}Subscription quota and account impact not reported`;
  }
  if (chargePath === "usage_credit") {
    const amount = amounts.length ? `${usd(total, !certain)} usage-credit value · ` : "";
    return `${amount}Provider credit-balance impact not reported`;
  }
  if (chargePath === "local_compute") {
    const amount = amounts.length ? `${usd(total, true)} API-equivalent reference · ` : "";
    return `${amount}Local hardware and energy cost not measured`;
  }
  const amount = amounts.length ? `${usd(total, true)} unclassified estimate · ` : "";
  return `${amount}Unknown charge path and account impact`;
}

/** Build one path-partitioned presentation; never turn missing cost into zero or "covered". */
export function billingPresentation(facts: readonly BillingFact[]): BillingPresentation {
  const groups = new Map<ChargePath, BillingFact[]>();
  for (const fact of facts) {
    groups.set(fact.chargePath, [...(groups.get(fact.chargePath) ?? []), fact]);
  }
  if (groups.size === 0) groups.set("unknown", []);
  const lines = [...groups.entries()].map(([chargePath, grouped]) => ({
    chargePath,
    count: grouped.length,
    text: describePath(chargePath, grouped),
  }));
  const heading = lines.length > 1 ? "Mixed billing paths" : "Billing path";
  return {
    heading,
    lines,
    ariaSummary: `${heading}: ${lines.map((line) => line.text).join("; ")}`,
  };
}

/** The complete hover copy for one run, including the same charge-path semantics as every board. */
export function runTooltip(run: Omit<AgentRun, "status"> & { status?: string }): string {
  const billing = billingPresentation([{
    chargePath: run.charge_path ?? "unknown",
    costCertainty: run.cost_certainty ?? "unknown",
    costUsd: run.cost_usd ?? null,
  }]).lines[0].text;
  return [
    `**${run.name}** — ${run.status ?? "unknown"}`,
    run.task ? `\n${run.task}\n` : "",
    `- provider: \`${run.provider}\`${run.model ? ` · model: \`${run.model}\`` : ""}`,
    `- project: \`${run.cwd || "—"}\``,
    `- billing: ${billing}`,
    `- id: \`${run.run_id}\``,
    run.foreign ? "\n_Not started by interact — one of your own sessions._" : "",
  ].join("\n");
}
