/** The tab strip above a conversation: who else is on this errand.
 *
 *  "When we call an agent, while it's working, the main agent would / could do things... make
 *  small tabs in the conversation so we can view how the agent is doing, if it's calling other
 *  agents etc.. And we should be able to come back to the main agent."
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { sessionTabs } from "./sessionTabs.ts";

const run = (over: Record<string, unknown> = {}) => ({
  run_id: "r", name: "claude", provider: "claude", status: "running", started_at: 100, ...over,
}) as never;

test("the entry agent is always the first tab, whichever depth you are at", () => {
  const runs = [
    run({ run_id: "main", name: "main" }),
    run({ run_id: "kid", name: "tester", parent_run_id: "main" }),
  ];
  for (const here of ["main", "kid"]) {
    const tabs = sessionTabs(runs, here, (r) => r.name);
    assert.equal(tabs[0].runId, "main", `at ${here} the way home must be the first tab`);
    assert.equal(tabs[0].root, true);
  }
});

test("each agent the session put to work gets a tab, marked live while it works", () => {
  const tabs = sessionTabs([
    run({ run_id: "main", name: "main" }),
    run({ run_id: "a", name: "tester", parent_run_id: "main", status: "running" }),
    run({ run_id: "b", name: "artist", parent_run_id: "main", status: "done" }),
  ], "main", (r) => r.name);
  assert.deepEqual(tabs.map((t) => t.label), ["main", "tester", "artist"]);
  assert.deepEqual(tabs.map((t) => t.live), [true, true, false]);
  assert.equal(tabs.find((t) => t.runId === "main")!.here, true);
});

test("a grandchild is a tab too, and says whose errand it is", () => {
  /* "if it's calling other agents" — depth is the whole point: an agent that spawned its own
     helpers must show them, or the tab strip lies about what the session is doing. */
  const tabs = sessionTabs([
    run({ run_id: "main", name: "main" }),
    run({ run_id: "a", name: "tester", parent_run_id: "main" }),
    run({ run_id: "a1", name: "explore", parent_run_id: "a" }),
  ], "a1", (r) => r.name);
  assert.deepEqual(tabs.map((t) => t.runId), ["main", "a", "a1"]);
  assert.equal(tabs[2].depth, 2);
  assert.equal(tabs[2].here, true);
});

test("a lone conversation gets no strip — chrome that says nothing is noise", () => {
  assert.deepEqual(sessionTabs([run({ run_id: "solo" })], "solo", (r) => r.name), []);
});

test("your own editor sessions never join somebody else's strip", () => {
  const tabs = sessionTabs([
    run({ run_id: "main", name: "main" }),
    run({ run_id: "kid", name: "tester", parent_run_id: "main" }),
    run({ run_id: "mine", status: "foreign" }),
  ], "main", (r) => r.name);
  assert.deepEqual(tabs.map((t) => t.runId), ["main", "kid"]);
});

test("the strip is capped, and says how many it could not show", () => {
  const kids = Array.from({ length: 12 }, (_, i) =>
    run({ run_id: `k${i}`, name: `a${i}`, parent_run_id: "main", started_at: 100 + i }));
  const tabs = sessionTabs([run({ run_id: "main", name: "main" }), ...kids], "main", (r) => r.name);
  assert.ok(tabs.length <= 8, "a strip wider than the panel is a scrollbar, not a tab strip");
  assert.equal(tabs[tabs.length - 1].more, kids.length - (tabs.length - 2),
    "the overflow tab counts exactly what it hides");
});
