import { strict as assert } from "node:assert";
import { test } from "node:test";

import { selectedRunId } from "./workplaceMessage.ts";

// Clicking a worker in the room did nothing at all — live-confirmed by an independent critic. The
// hook was present on both sides: the scene posts `select`/`run_id`, the host listened for
// `focus`/`runId`. Two halves built to one contract that was never written down.

test("the pixel-art scene's message selects a worker", () => {
  assert.equal(selectedRunId({ type: "select", run_id: "abc" }), "abc");
});

test("the plain fallback's message selects a worker too", () => {
  assert.equal(selectedRunId({ type: "focus", runId: "abc" }), "abc");
});

test("anything else selects nobody", () => {
  for (const msg of [null, undefined, {}, { type: "select" }, { type: "focus", runId: "" },
                     { type: "other", run_id: "abc" }, { run_id: "abc" }]) {
    assert.equal(selectedRunId(msg), null, `unexpected selection from ${JSON.stringify(msg)}`);
  }
});
