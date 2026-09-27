import type { ConversationCatalog } from "./generated/types";
import type { AgentRun } from "./agents";
import type { Org } from "./org";
import type { CellContent } from "./shared";

interface BenchmarkEvidence { model_id?: string | null; score: number; status?: string }
interface BenchmarkSource { name: string; source: string; published?: { entries: BenchmarkEvidence[] } | null }

export function comparisonRows(
  organization: Org | null,
  catalog: ConversationCatalog | undefined,
  benchmarks: BenchmarkSource[],
  runs: AgentRun[],
): CellContent[] {
  const agents = organization?.agents ?? [];
  return agents.map((agent) => {
    const declaredProviders = agent.providers ?? [];
    const routes = catalog?.routes.filter((route) => declaredProviders.includes(route.provider)) ?? [];
    const available = routes.filter((route) => route.availability === "available");
    const model = agent.model ?? "unknown";
    const evidence = benchmarks.flatMap((benchmark) => (benchmark.published?.entries ?? [])
      .filter((entry) => entry.model_id === model && entry.status === "eligible")
      .map((entry) => `${benchmark.name}: ${entry.score} (${benchmark.source})`));
    const actual = runs.filter((run) => run.agent === agent.name).at(-1);
    const routeText = available.length
      ? available.map((route) => `${route.provider}/${route.connection}/${route.charge_path}; ${(route.capabilities ?? []).join(", ")}`).join(" · ")
      : routes.length ? "connected, conversation route unsupported" : "no supported conversation route";
    return {
      kind: "row", label: agent.name,
      value: `${model} · ${routeText} · evidence ${evidence.join("; ") || "unknown"} · latest ${
        actual ? `${actual.provider}/${actual.status}/${actual.charge_path ?? "unknown"}` : "unknown"
      }`,
      tooltip: agent.description ?? undefined,
    };
  });
}
