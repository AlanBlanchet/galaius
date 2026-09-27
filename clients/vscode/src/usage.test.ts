import { strict as assert } from "node:assert";
import { test } from "node:test";

import {
  aggregateUsageProviders,
  summarizeUsage,
  type UsageEntry,
} from "./usageSummary.ts";

const mixed: UsageEntry[] = [
  {
    timestamp: "2026-08-27T10:00:00Z",
    model: "claude default",
    backend: "session",
    provider: "claude",
    billing: "session_usage",
    outcome: "succeeded",
    input_tokens: 12,
    output_tokens: 3,
    cost: null,
  },
  {
    timestamp: "2026-08-27T10:01:00Z",
    model: "openai/example-model",
    backend: "api",
    provider: "openai",
    billing: "metered_api",
    outcome: "succeeded",
    input_tokens: 8,
    output_tokens: 2,
    cost: 0.25,
  },
];

test("provider grouping uses persisted session identity and preserves unknown impact", () => {
  const groups = aggregateUsageProviders(mixed);
  const claude = groups.find((group) => group.provider === "claude");
  const openai = groups.find((group) => group.provider === "openai");
  assert.deepEqual(claude, {
    provider: "claude",
    cost: 0,
    unknownCostCalls: 1,
    sessionUsageCalls: 1,
  });
  assert.equal(openai?.cost, 0.25);
});

test("a historical row without provider stays unknown", () => {
  const historical = { ...mixed[1], provider: undefined };
  assert.equal(aggregateUsageProviders([historical])[0].provider, "unknown");
});

test("dashboard summary separates observed API dollars from unknown session account impact", () => {
  const failedApi: UsageEntry = {
    timestamp: "2026-08-27T10:00:00Z", model: "openai/example", provider: "openai",
    backend: "api", billing: "metered_api", outcome: "failed",
    input_tokens: 0, output_tokens: 0, cost: null,
  };
  assert.deepEqual(summarizeUsage([...mixed, failedApi]), {
    observedMeteredApiCost: 0.25,
    unknownAccountImpactCalls: 2,
    unknownMeteredApiCostCalls: 1,
    sessionUsageCalls: 1,
  });
});
