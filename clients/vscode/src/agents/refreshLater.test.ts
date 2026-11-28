/** A refresh asked after a burst of writes is never held back past its cap, and one run read serves a whole synchronous turn. */
import { strict as assert } from "node:assert";
import { createRequire } from "node:module";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import { outOf, TEST_OUT } from "../testPaths.ts";

const require_ = createRequire(import.meta.url);
const built = path.join(outOf(import.meta.url), "agents.js");

test("writes faster than the wait still refresh within the cap, and a quiet burst refreshes after the wait", async (t) => {
  if (!fs.existsSync(built)) return t.skip("run npm run compile first");
  const { refreshLater } = require_(built);
  const started = Date.now();
  let ranAt: number[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  for (let write = 0; write < 30; write++) {   // a write every 60 ms for 1.8 s, wait 200 ms, cap 500 ms
    timer = refreshLater(timer, () => ranAt.push(Date.now() - started), 200, 500);
    await new Promise((resolve) => setTimeout(resolve, 60));
  }
  assert.ok(ranAt.length >= 2 && ranAt[0] < 700, `first refresh at ${ranAt[0]} ms: ${ranAt}`);
  await new Promise((resolve) => setTimeout(resolve, 400));
  const before = ranAt.length;
  ranAt = [];
  const quiet = Date.now();
  timer = refreshLater(timer, () => ranAt.push(Date.now() - quiet), 100, 500);
  timer = refreshLater(timer, () => ranAt.push(Date.now() - quiet), 100, 500);
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(ranAt.length, 1, `a burst of two runs once (${before} earlier): ${ranAt}`);
  assert.ok(ranAt[0] >= 90 && ranAt[0] < 250, String(ranAt[0]));
});

test("one registry read serves every caller of a turn, the next turn reads again", async (t) => {
  if (!fs.existsSync(built)) return t.skip("run npm run compile first");
  const root = path.join(TEST_OUT, `refresh-later-${process.pid}-${Date.now()}`);
  const directory = path.join(root, ".galaius", "out", "agents");
  fs.mkdirSync(directory, { recursive: true });
  const previousHome = process.env.HOME;
  process.env.HOME = root;
  try {
    delete require_.cache[require_.resolve(built)];
    const { readAgentRuns } = require_(built);
    const write = (id: string) => fs.writeFileSync(path.join(directory, `${id}.json`), JSON.stringify({
      run_id: id, kind: "conversation", provider: "p", name: id, status: "done", root_run_id: id, connection: "local_session",
      cataloged_at: 1, charge_path: "subscription_quota", cost_certainty: "unknown", capabilities: [], tools: [],
    }));
    write("a");
    const first = readAgentRuns();
    write("b");
    assert.equal(readAgentRuns().length, first.length, "the same turn: the same read");
    assert.notEqual(readAgentRuns(), readAgentRuns(), "callers get their own array");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(readAgentRuns().length, first.length + 1, "the next turn reads again");
  } finally {
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    fs.rmSync(root, { recursive: true });
  }
});
