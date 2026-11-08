import assert from "node:assert/strict";
import { test } from "node:test";
import { addCounts, latestCounts, olderWritten, promptTokens } from "./tokenCounts.ts";

test("a run's tokens add figure by figure; its prompt holds its cached part; nothing reported stays absent", () => {
  const summed = addCounts({ input: 10, output: 5, cache_read: 100 }, { input: 20, cache_write: 4 });
  assert.deepEqual(summed, { input: 30, output: 5, cache_read: 100, cache_write: 4, reasoning: null });
  assert.equal(promptTokens(summed), 134);
  assert.equal(promptTokens(undefined), null);
  assert.deepEqual(latestCounts({ input: 30, output: 2 }, { input: 10, output: 9 }), { input: 30, output: 9, cache_read: null, cache_write: null, reasoning: null });
  assert.deepEqual([latestCounts(null, { input: 1 }), latestCounts(undefined, null)], [{ input: 1 }, undefined]);
});

test("what an older runtime wrote (run record, event line, usage-log row) reads in the one spelling; a new row passes", () => {
  assert.deepEqual(olderWritten({ model: "m", input_tokens: 12, output_tokens: 3 }), { model: "m", usage: { input: 12, output: 3 } });
  assert.deepEqual(olderWritten({ run_id: "r", input_tokens: 90000, output_tokens: 3000, cached_input_tokens: 85000, cache_write_input_tokens: 1000 }),
    { run_id: "r", usage: { input: 4000, output: 3000, cache_read: 85000, cache_write: 1000 } });
  assert.deepEqual(olderWritten({ run_id: "r", input_tokens: null, output_tokens: null }), { run_id: "r" });
  const window = { window: "five_hour", used_percent: 40, observed_at: "2026-10-20T10:00:00Z" };
  assert.deepEqual(olderWritten({ kind: "rate_limit", usage: [window] }), { kind: "rate_limit", windows: [window] });
  assert.deepEqual(olderWritten({ model: "m", usage: { input: 1 } }), { model: "m", usage: { input: 1 } });
});
