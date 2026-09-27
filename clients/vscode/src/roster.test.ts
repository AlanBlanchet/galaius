/** His words: "an agent is different than a conversation with an agent... When i click on an agent,
 *  i should be able to view the conversations is had, and go back to the parent if there is one."
 *  And, separately: "The names are not correct."
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  agentLabel, agentsFrom, ancestry, childrenOf, conversationTitle, roleOf, type Run,
} from "./roster.ts";

const run = (o: Partial<Run> & { run_id: string }): Run => ({ provider: "claude", ...o });

test("a conversation is named by what it is DOING, not by the CLI that ran it", () => {
  /* The bug he reported. Python records `name` as the provider when a run has no definition, so
     the panel showed a column of identical "claude" rows for completely unrelated work. */
  const r = run({ run_id: "abc12345", name: "claude", provider: "claude", task: "Fix the uinput attach race" });
  assert.equal(conversationTitle(r), "Fix the uinput attach race");
});

test("launch-role routing is not the conversation's visible task", () => {
  const r = run({ run_id: "role-brief", agent: "app-engineer", task: "AGENT_ROLE: app-engineer. Improve the workflow editor" });
  assert.equal(conversationTitle(r), "Improve the workflow editor");
  assert.equal(r.task, "AGENT_ROLE: app-engineer. Improve the workflow editor");
  assert.equal(conversationTitle(run({ run_id: "literal", task: "AGENT_ROLE: tester is part of this example" })), "AGENT_ROLE: tester is part of this example");
});

test("a name that merely repeats the CLI is not a name", () => {
  const r = run({ run_id: "abc12345", name: "claude", provider: "claude" });
  /* A bare id-slice reads as a typo, not an identifier — a critic mistook "run-fore" for one. */
  assert.equal(conversationTitle(r), "untitled · abc123",
    "should say what it is rather than repeat the CLI or emit a bare id fragment");
});

test("a long task is truncated so a roster stays scannable", () => {
  const t = "x".repeat(200);
  const got = conversationTitle(run({ run_id: "a", task: t }));
  assert.ok(got.length <= 60, `title was ${got.length} chars`);
  assert.ok(got.endsWith("…"));
});

test("a real definition names the role; a bare CLI is marked plain", () => {
  assert.deepEqual(roleOf(run({ run_id: "a", agent: "visual-critic" })), { id: "visual-critic", plain: false });
  assert.deepEqual(roleOf(run({ run_id: "b", agent: null, provider: "claude" })), { id: "claude", plain: true });
});

test("one agent holds many conversations", () => {
  const runs = [
    run({ run_id: "1", agent: "tester", task: "verify chords" }),
    run({ run_id: "2", agent: "tester", task: "verify delivery" }),
    run({ run_id: "3", agent: "researcher", task: "price check" }),
  ];
  const agents = agentsFrom(runs);
  assert.equal(agents.length, 2);
  const tester = agents.find((a) => a.id === "tester")!;
  assert.equal(tester.conversations.length, 2, "both engagements belong to the same role");
  assert.deepEqual(tester.conversations.map(conversationTitle), ["verify chords", "verify delivery"]);
});

test("you can walk up to the parent, and to its parent", () => {
  const runs = [
    run({ run_id: "child", parent_run_id: "mid", task: "c" }),
    run({ run_id: "mid", parent_run_id: "root", task: "m" }),
    run({ run_id: "root", task: "r" }),
  ];
  assert.deepEqual(ancestry("child", runs).map((r) => r.run_id), ["mid", "root"]);
  assert.deepEqual(ancestry("root", runs), [], "a root conversation has nowhere to go up to");
});

test("a malformed parent link cannot hang the panel", () => {
  /* These ids are written by several processes; a cycle must degrade, never spin. */
  const runs = [
    run({ run_id: "a", parent_run_id: "b" }),
    run({ run_id: "b", parent_run_id: "a" }),
  ];
  const chain = ancestry("a", runs);
  assert.ok(chain.length <= 2, "cycle was not bounded");
});

test("a parent outside the current scope stops the chain rather than inventing a row", () => {
  const runs = [run({ run_id: "a", parent_run_id: "elsewhere" })];
  assert.deepEqual(ancestry("a", runs), []);
});

test("a conversation knows what it spawned", () => {
  const runs = [
    run({ run_id: "lead", task: "lead" }),
    run({ run_id: "k1", parent_run_id: "lead", task: "one" }),
    run({ run_id: "k2", parent_run_id: "lead", task: "two" }),
    run({ run_id: "other", task: "unrelated" }),
  ];
  assert.deepEqual(childrenOf("lead", runs).map((r) => r.run_id), ["k1", "k2"]);
});

test("a definition-less run resolves to the coordinator, not to the binary name", () => {
  /* His words: "We also have some agents called 'claude' instead of having their agent name."

     The librarian's finding is that the bare session is NOT file-less — the main thread's system
     prompt IS `instructions.md`, so the coordinator is its definition. The org file now says so
     explicitly (`matches: "definition-less"`, and a `binary` token per provider), and this is the
     consumer half: a run with no agent whose recorded name is just the CLI's binary is the
     coordinator, and should say so. */
  const company = {
    coordinator: { id: "main", title: "Main thread — session coordinator" },
    binaries: ["claude", "codex"],
  };
  const bare = { run_id: "a", provider: "claude", name: "claude", agent: null };
  assert.deepEqual(roleOf(bare, company), { id: "main", plain: false });
  assert.equal(agentLabel(bare, company), "Main thread — session coordinator");
});

test("a real definition still wins over the coordinator fallback", () => {
  const company = { coordinator: { id: "main", title: "Main" }, binaries: ["claude"] };
  const run = { run_id: "b", provider: "claude", name: "claude", agent: "visual-critic" };
  assert.deepEqual(roleOf(run, company), { id: "visual-critic", plain: false });
});

test("without a company file nothing is invented", () => {
  /* interact must work for someone with no prompt repo at all — then the provider IS all we know. */
  const bare = { run_id: "c", provider: "claude", name: "claude", agent: null };
  assert.deepEqual(roleOf(bare), { id: "claude", plain: true });
});

test("a name that is not a known binary is left alone", () => {
  const company = { coordinator: { id: "main", title: "Main" }, binaries: ["claude"] };
  const run = { run_id: "d", provider: "claude", name: "my-own-thing", agent: null };
  assert.equal(roleOf(run, company).id, "my-own-thing", "only a BINARY name means 'the CLI itself'");
});
