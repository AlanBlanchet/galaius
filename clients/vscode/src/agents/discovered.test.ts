import { test } from "node:test";
import assert from "node:assert/strict";
import { parseDiscovered, mergeDiscovered, isFresh, DISCOVERY_TTL_MS } from "./discovered.ts";
import type { AgentRun } from "./agents.ts";

const LINE = (over: Record<string, unknown> = {}) => JSON.stringify({
  run_id: "s-1", name: "sheets-ab", provider: "claude", cwd: "/workspace/xp/sheets",
  project: "sheets", pid: 4242, started_at: 10, last: "interactive",
  status: "foreign", foreign: true, ...over,
});

test("each line becomes a session the panel can show", () => {
  const runs = parseDiscovered([LINE(), LINE({ run_id: "s-2", name: "sheets-b1" })].join("\n"));
  assert.deepEqual(runs.map((r) => r.name), ["sheets-ab", "sheets-b1"]);
  assert.equal(runs[0].project, "sheets");
  assert.equal(runs[0].status, "foreign");
});

test("a discovered session is marked foreign even if the payload forgot to", () => {
  // The panel styles and filters on this: teamSpend excludes foreign runs so someone else's
  // editor window does not deflate the team's cost shares, and the tree labels it "not ours".
  const [run] = parseDiscovered(LINE({ foreign: undefined }));
  assert.equal(run.foreign, true);
});

test("a line that is not a session is skipped, not fatal", () => {
  // A warning printed on stdout by anything in the chain would otherwise cost every session.
  const runs = parseDiscovered(["oops, a warning", LINE(), "{bad json"].join("\n"));
  assert.deepEqual(runs.map((r) => r.run_id), ["s-1"]);
  assert.deepEqual(parseDiscovered(""), []);
});

test("a payload with no run_id is not a session", () => {
  assert.deepEqual(parseDiscovered(JSON.stringify({ name: "nameless" })), []);
});

const onDisk = (over: Partial<AgentRun> = {}): AgentRun => ({
  run_id: "r-1", provider: "claude", name: "tester", status: "done",
  started_at: 5, cost_usd: 1.5, ...over,
} as AgentRun);

test("discovered sessions join the ones on disk, in start order", () => {
  const merged = mergeDiscovered([onDisk()], parseDiscovered(LINE()));
  assert.deepEqual(merged.map((r) => r.name), ["tester", "sheets-ab"]);
});

test("a run that is both on disk and discovered keeps its RECORD", () => {
  // The record carries cost, tokens, the definition, the parent. Letting the discovered stub win
  // would replace a fully-described run with a bare one and read as data loss.
  const merged = mergeDiscovered([onDisk({ run_id: "s-1", name: "tester" })], parseDiscovered(LINE()));
  assert.equal(merged.length, 1);
  assert.equal(merged[0].name, "tester");
  assert.equal(merged[0].cost_usd, 1.5);
});

test("a cached discovery is reused briefly, then re-asked", () => {
  const cache = { runs: [], at: 1_000 };
  assert.equal(isFresh(cache, 1_000 + DISCOVERY_TTL_MS - 1), true);
  assert.equal(isFresh(cache, 1_000 + DISCOVERY_TTL_MS), false);
  assert.equal(isFresh(null, 1_000), false, "no cache is never fresh");
});
