import { strict as assert } from "node:assert";
import { test } from "node:test";
import { comparisonRows } from "./comparison.ts";

test("comparison separates runnable Codex from connected external sessions and unknown evidence", () => {
  const rows = comparisonRows({
    coordinator: { id: "main" }, departments: [], providers: {}, agents: [
      { name: "builder", providers: ["codex"], model: "openai/gpt-5" },
      { name: "reader", providers: ["claude", "gemini"], model: "anthropic/opus" },
    ],
  }, {
    version: 1, cataloged_at: 1, criteria: [], routes: [
      { id: "codex", provider: "codex", connection: "local_session", label: "Codex",
        availability: "available", reason: "", charge_path: "subscription_quota",
        cost_certainty: "unknown", billing_note: "Account impact unknown", authenticated: true,
        capabilities: ["streaming", "resume"], models: [], cataloged_at: 1 },
      { id: "claude", provider: "claude", connection: "local_session", label: "Claude",
        availability: "unsupported", reason: "read only", charge_path: "unknown",
        cost_certainty: "unknown", billing_note: "Unsupported", authenticated: true,
        capabilities: [], models: [], cataloged_at: 1 },
    ],
  } as any, [], [{ agent: "reader", provider: "claude", status: "foreign",
    charge_path: "unknown" } as any]);
  const text = JSON.stringify(rows);
  assert.match(text, /builder.*codex\/local_session\/subscription_quota.*streaming.*evidence unknown/i);
  assert.match(text, /reader.*connected, conversation route unsupported.*latest claude\/foreign\/unknown/i);
  assert.doesNotMatch(text, /reader.*api/i);
});
