import { strict as assert } from "node:assert";
import { test } from "node:test";
import { buildSequence } from "./sequenceFormat.ts";


test("only agents who took part get a lane; the silent are a count", () => {
  /* The professional sweep found 17 lanes allocated for 2 participants, the rest off-screen — a
     diagram whose subject was hidden by its own cast list. A lane is earned by participating:
     spawning, being spawned, or exchanging a message. */
  const runs = Array.from({ length: 17 }, (_, i) => ({
    run_id: `r${i}`, name: `agent${i}`, provider: "claude", status: "done", started_at: i,
  }));
  const messages = [{ from_run: "r3", to_run: "r7", text: "take the panel work" }];
  const seq = buildSequence(runs as never[], messages as never[]);
  assert.deepEqual(seq.lanes.map((l) => l.run_id).sort(), ["r3", "r7"]);
  assert.equal(seq.silent, 15, "the rest are a count, not fifteen empty columns");
});

test("with no interactions at all, the cast still shows rather than an empty stage", () => {
  const runs = [
    { run_id: "a", name: "a", provider: "claude", status: "done", started_at: 1 },
    { run_id: "b", name: "b", provider: "claude", status: "running", started_at: 2 },
  ];
  const seq = buildSequence(runs as never[], []);
  assert.equal(seq.lanes.length, 2, "a team that never spoke is still a team");
  assert.equal(seq.silent, 0);
});
