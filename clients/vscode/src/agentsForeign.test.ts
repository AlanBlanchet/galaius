/** Your own sessions, readable.
 *
 *  A foreign run's id is its Claude session id, and the provider files the transcript under a
 *  munged-cwd directory. Mapping that shape into ours is what turns a "your session" row from a
 *  grey dead thing into a place the panel can actually show.
 */
import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { foreignTranscriptPath, fullToolIO, readForeignActivity } from "./foreignSession.ts";

test("the transcript path is the provider's munged-cwd convention", () => {
  const p = foreignTranscriptPath({ run_id: "sid-1", cwd: "/tmp/project/dev/my.app" }, "/HOME");
  assert.equal(p, "/HOME/.claude/projects/-tmp-project-dev-my-app/sid-1.jsonl");
  assert.equal(foreignTranscriptPath({ run_id: "x", cwd: "" }), null,
    "no working directory, no place to look");
});

test("a session's tail maps into the conversation grammar", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "foreign-"));
  const dir = path.join(home, ".claude", "projects", "-tmp-proj");
  fs.mkdirSync(dir, { recursive: true });
  const lines = [
    { type: "user", timestamp: "2026-08-26T10:00:00Z",
      message: { role: "user", content: "fix the panel" } },
    { type: "assistant", timestamp: "2026-08-26T10:00:05Z",
      message: { role: "assistant", content: [
        { type: "thinking", thinking: "the rail is the object" },
        { type: "text", text: "On it." },
        { type: "tool_use", name: "Bash", input: { command: "ls" } },
      ] } },
    { type: "user", timestamp: "2026-08-26T10:00:09Z",
      message: { role: "user", content: [
        { type: "tool_result", content: [{ type: "text", text: "a.ts\nb.ts" }] },
      ] } },
    { type: "progress" }, // housekeeping shapes are skipped, never crashed on
  ];
  fs.writeFileSync(path.join(dir, "sess.jsonl"),
    lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  const turns = readForeignActivity({ run_id: "sess", cwd: "/tmp/proj" }, 40, home);
  assert.deepEqual(turns.map((t) => t.kind), ["prompt", "thinking", "text", "tool", "tool_result"]);
  assert.equal(turns[0].text, "fix the panel");
  assert.equal(turns[3].tool, "Bash");
  assert.match(turns[3].tool_input ?? "", /"command":"ls"/);
  assert.equal(turns[4].text, "a.ts\nb.ts");
  assert.ok((turns[0].at ?? 0) > 1_700_000_000, "the provider's timestamp becomes our clock");
});

test("a missing transcript is an empty conversation, never a crash", () => {
  assert.deepEqual(readForeignActivity({ run_id: "ghost", cwd: "/nowhere" }, 40, "/no-such-home"), []);
});

test("a tool call carries the vendor's id on both halves", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "foreign-id-"));
  const dir = path.join(home, ".claude", "projects", "-w");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "s.jsonl"), [
    JSON.stringify({ type: "assistant", message: { content: [
      { type: "tool_use", id: "toolu_9", name: "Bash", input: { command: "ls" } }] } }),
    JSON.stringify({ type: "user", message: { content: [
      { type: "tool_result", tool_use_id: "toolu_9", content: "a" }] } }),
  ].join("\n"));
  const turns = readForeignActivity({ run_id: "s", cwd: "/w" }, 10, home);
  assert.equal(turns[0].tool_id, "toolu_9");
  assert.equal(turns[1].tool_id, "toolu_9", "the result names the question it answers");
});

test("the whole input and output of one call come out of the raw stream by id", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "rawio-"));
  const file = path.join(dir, "run.raw.jsonl");
  const big = "line\n".repeat(4000); // far past the 2000-char summary clip
  fs.writeFileSync(file, [
    JSON.stringify({ type: "assistant", message: { content: [
      { type: "tool_use", id: "toolu_A", name: "Bash",
        input: { command: "npm test", timeout: 90000 } }] } }),
    JSON.stringify({ type: "user", message: { content: [
      { type: "tool_result", tool_use_id: "toolu_A",
        content: [{ type: "text", text: big }] }] } }),
    JSON.stringify({ type: "assistant", message: { content: [
      { type: "tool_use", id: "toolu_B", name: "Bash", input: { command: "npm test" } }] } }),
  ].join("\n"));
  const input = fullToolIO(file, "toolu_A", "in");
  assert.match(input ?? "", /"command": "npm test"/);
  assert.match(input ?? "", /"timeout": 90000/, "the WHOLE input, not the summarised lead args");
  const output = fullToolIO(file, "toolu_A", "out");
  assert.equal(output, big, "nothing clipped — that is the entire point of the tab");
  assert.match(fullToolIO(file, "toolu_B", "in") ?? "", /npm test/,
    "two identical commands stay distinguishable — the id pairs them, never the text");
  assert.equal(fullToolIO(file, "toolu_MISSING", "out"), null);
});
