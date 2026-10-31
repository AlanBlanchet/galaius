/** The sequence view's layout — lanes for agents, time down, arrows between them.
 *
 *  Layout is where this kind of diagram goes quietly wrong: an arrow drawn to a lane that does
 *  not exist, or a participant silently dropped. Both look fine until the one exchange you cared
 *  about is the missing one.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { buildSequence } from "./sequenceFormat.ts";

const runs = [
  { run_id: "a", name: "lead", provider: "claude", status: "done", started_at: 1 },
  { run_id: "b", name: "reviewer", provider: "claude", status: "running", started_at: 2 },
  {
    run_id: "c",
    name: "perf",
    provider: "codex",
    status: "done",
    started_at: 3,
    parent_run_id: "a",
  },
] as never[];

test("participants get a lane in start order; the silent are a count", () => {
  /* The rule changed with the professional sweep: seventeen columns for two participants hid the
     diagram's subject behind its cast list. Here the reviewer neither spawned, was spawned, nor
     spoke — so it is the count, not an empty column. */
  const s = buildSequence(runs, []);
  assert.deepEqual(s.lanes.map((l) => l.name), ["lead", "perf"]);
  assert.equal(s.silent, 1);
});

test("a message becomes an arrow between the right lanes", () => {
  const s = buildSequence(runs, [{ from_run: "a", to_run: "b", text: "review this" }]);
  const arrow = s.arrows.find((x) => x.kind === "message");
  assert.ok(arrow, "the message was not drawn");
  assert.equal(arrow.fromLane, 0);
  assert.equal(arrow.toLane, 1);
  assert.ok(arrow.label.includes("review this"));
});

test("a spawn is an arrow too — it is how a team actually forms", () => {
  const s = buildSequence(runs, []);
  const spawn = s.arrows.find((x) => x.kind === "spawn");
  assert.ok(spawn, "the parent/child relationship was not drawn");
  assert.equal(spawn.fromLane, 0);
  // Lane 1 now, not 2: the silent reviewer no longer holds a column between them.
  assert.equal(spawn.toLane, 1);
});

test("an arrow to an unknown run is dropped, not drawn to nowhere", () => {
  const s = buildSequence(runs, [{ from_run: "a", to_run: "ghost", text: "hi" }]);
  assert.equal(s.arrows.filter((x) => x.kind === "message").length, 0);
});

test("an arrow whose target has no lane never collapses onto the sender", () => {
  // Pointing it at lane 0 would render as the agent talking to itself — a lie, not a fallback.
  const s = buildSequence([runs[0]] as never[], [{ from_run: "a", to_run: "b", text: "hi" }]);
  assert.equal(s.arrows.length, 0);
});

test("no agents is an explicit empty state, not a blank diagram", () => {
  assert.equal(buildSequence([], []).lanes.length, 0);
});

test("arrows keep their order — a sequence read out of order is not a sequence", () => {
  const s = buildSequence(runs, [
    { from_run: "a", to_run: "b", text: "first" },
    { from_run: "b", to_run: "a", text: "second" },
  ]);
  const msgs = s.arrows.filter((x) => x.kind === "message");
  assert.ok(msgs[0].label.includes("first"));
  assert.ok(msgs[1].label.includes("second"));
  assert.ok(msgs[1].y > msgs[0].y, "later exchanges must sit lower on the time axis");
});
