/** What the team is costing, and this agent's share of it.
 *
 *  The chat showed one agent's tokens and cost and nothing about the whole. For someone who owns
 *  the budget that is the wrong half: the number that matters is what the team is spending, and
 *  whether the agent you happen to be reading is a large part of it.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { teamSpend } from "./teamSpend.ts";
import type { AgentRun } from "./agents.ts";

const runs = [
  { run_id: "a", cost_usd: 3, status: "running" },
  { run_id: "b", cost_usd: 1, status: "done" },
  { run_id: "c", cost_usd: null, status: "running" },
  { run_id: "d", cost_usd: 0.5, status: "foreign", foreign: true },
] as AgentRun[];

test("the team total is what interact actually spawned", () => {
  // A foreign run is the user's own editor session — real money, but not this team's, and
  // counting it would inflate every share below it.
  const s = teamSpend(runs, "a");
  assert.equal(s.total, 4);
  assert.equal(s.agents, 3);
});

test("the open agent's share is stated, because that is the actionable half", () => {
  assert.equal(teamSpend(runs, "a").sharePercent, 75);
  assert.equal(teamSpend(runs, "b").sharePercent, 25);
});

test("an agent whose cost is not known yet claims no share", () => {
  // `null` is unknown, not zero — reporting 0% would say "this one is free", which is a claim.
  const s = teamSpend(runs, "c");
  assert.equal(s.sharePercent, null);
});

test("a team with no reported costs has neither a total nor shares", () => {
  const s = teamSpend([{ run_id: "a", cost_usd: null, status: "running" }] as AgentRun[], "a");
  assert.equal(s.total, null);
  assert.equal(s.sharePercent, null);
});

test("it counts who is still working, since that is what is still spending", () => {
  assert.equal(teamSpend(runs, "a").running, 2);
});

test("terminal lifecycle and known accounting survive either snapshot order", () => {
  const known = {
    run_id: "root", status: "done", cost_usd: 2.5,
    input_tokens: 20, output_tokens: 10,
  } as AgentRun;
  const stale = {
    run_id: "root", status: "running", cost_usd: null,
    input_tokens: 4, output_tokens: 2,
  } as AgentRun;
  for (const [stored, current] of [[known, stale], [stale, known]] as const) {
    const spend = teamSpend([stored], "root", [current]);
    assert.deepEqual(
      { total: spend.total, running: spend.running },
      { total: 2.5, running: 0 },
      "stale lifecycle or null accounting must not overwrite terminal known facts",
    );
  }
});
