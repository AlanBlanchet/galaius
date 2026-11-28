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

test("writes every 100 ms refresh once a second, counted from the previous refresh's start, render included", async (t) => {
  if (!fs.existsSync(built)) return t.skip("run npm run compile first");
  const { refreshLater } = require_(built);
  const started = Date.now();
  const ranAt: number[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  const render = () => {   // 300 ms of work, as a panel's render takes
    ranAt.push(Date.now() - started);
    const until = Date.now() + 300;
    while (Date.now() < until);
  };
  for (let write = 0; write < 50; write++) {   // a write every 100 ms for 5 s
    timer = refreshLater(timer, render, 250, 1000);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const gaps = ranAt.slice(1).map((at, index) => at - ranAt[index]);
  assert.ok(ranAt.length >= 5 && gaps.every((gap) => gap <= 1150), `refreshes at ${ranAt}, gaps ${gaps}`);
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

test("the deduplicated messages are kept while no message file changed and follow every change", (t) => {
  if (!fs.existsSync(built)) return t.skip("run npm run compile first");
  const root = path.join(TEST_OUT, `messages-memo-${process.pid}-${Date.now()}`);
  const directory = path.join(root, ".galaius", "out", "agents");
  fs.mkdirSync(directory, { recursive: true });
  const previousHome = process.env.HOME;
  process.env.HOME = root;
  try {
    delete require_.cache[require_.resolve(built)];
    const { readAgentMessages } = require_(built);
    const line = (text: string) => JSON.stringify({ from_run: "a", to_run: "b", text }) + "\n";
    const file = path.join(directory, "b.messages.jsonl");
    fs.writeFileSync(file, line("one") + line("one"));
    const first = readAgentMessages();
    assert.deepEqual(first.map((m: { text: string }) => m.text), ["one"], "a repeated exchange once");
    first.pop();
    assert.equal(readAgentMessages().length, 1, "a caller's change never reaches the kept result");
    fs.appendFileSync(file, line("two"));
    assert.deepEqual(readAgentMessages().map((m: { text: string }) => m.text), ["one", "two"]);
    fs.unlinkSync(file);
    assert.deepEqual(readAgentMessages(), []);
    fs.writeFileSync(file, line("three"));
    assert.equal(readAgentMessages().length, 1);
    fs.rmSync(directory, { recursive: true });   // the run folder gone: nothing is kept of it
    assert.deepEqual(readAgentMessages(), []);
  } finally {
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    fs.rmSync(root, { recursive: true });
  }
});
