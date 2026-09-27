/** The extension's view of the live model catalog.
 *
 *  Run: node --experimental-strip-types --test src/catalog.test.ts
 *
 *  These exist because the panel showed BUILD-TIME data and looked current. The whole point of
 *  the catalog is that it carries its own age, so the UI can say "3d ago" instead of quietly
 *  presenting a snapshot as today's truth.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { describeAge, isLive, seeingModels, type Catalog } from "./catalogFormat.ts";

function cat(over: Partial<Catalog> = {}): Catalog {
  return {
    source: "openrouter",
    fetched_at: Date.now() / 1000,
    models: [
      { id: "google/gemini-3.7-flash", name: "Gemini 3.7 Flash", context_length: 1048576, input_cost_per_token: 3.8e-7, input_modalities: ["text", "image", "video"] },
      { id: "anthropic/claude-sonnet-5", name: "Claude Sonnet 5", context_length: 1000000, input_cost_per_token: 3e-6, input_modalities: ["text", "image"] },
      { id: "tiny/text-only", name: "Tiny", context_length: 8192, input_cost_per_token: 1e-8, input_modalities: ["text"] },
    ],
    ...over,
  };
}

test("age is described for a human, not in epoch seconds", () => {
  assert.match(describeAge(5), /just now/i);
  assert.match(describeAge(3600 * 5), /h ago/);
  assert.match(describeAge(86400 * 4), /d ago/);
});

test("freshly fetched openrouter data is live", () => {
  assert.equal(isLive(cat()), true);
});

test("a stale cache is NOT live, however good the data is", () => {
  // This is the defect being fixed: aged numbers presented as current.
  assert.equal(isLive(cat({ fetched_at: Date.now() / 1000 - 86400 * 3 })), false);
});

test("the offline fallback is never live", () => {
  assert.equal(isLive(cat({ source: "litellm" })), false);
});

test("highlights prefer models that can actually see — this is a vision tool", () => {
  const picks = seeingModels(cat());
  assert.equal(picks.length, 2);
  assert.ok(picks.every((m) => m.input_modalities.includes("image")),
    "a text-only model is useless for driving a UI and must not be highlighted");
});

test("highlights survive a catalog with no modality data", () => {
  const bare = cat({ models: [{ id: "x", name: "x", context_length: null, input_cost_per_token: null, input_modalities: [] }] });
  assert.doesNotThrow(() => seeingModels(bare));
});
