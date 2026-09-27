/** What a chat surface accepts from its webview, and what it does with a reply.
 *
 *  Two chat surfaces now render the same document — the sidebar view and a full editor-area page —
 *  so the rules about what a message may ask for have to live in ONE place. They were inline in
 *  the sidebar's provider, which is how a second surface would have quietly grown a weaker copy.
 *
 *  The panel renders AGENT OUTPUT, so everything arriving from it is untrusted.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { chatAction } from "./chatMessage.ts";

const COMMANDS = [
  { slash: "/team", title: "Team", detail: "", command: "interact.agents.team", needsAgent: false },
  { slash: "/stop", title: "Stop", detail: "", command: "interact.agents.stop", needsAgent: true },
];

test("a reply carries its text through", () => {
  assert.deepEqual(chatAction({ type: "send", text: "look again" }, COMMANDS),
    { kind: "send", text: "look again" });
});

test("an empty or whitespace-only reply is not a message", () => {
  // Sending "" to an agent wakes it up to read nothing, which costs a turn and says nothing.
  assert.equal(chatAction({ type: "send", text: "   " }, COMMANDS), null);
  assert.equal(chatAction({ type: "send", text: "" }, COMMANDS), null);
});

test("only a command the panel actually declares is accepted", () => {
  assert.deepEqual(chatAction({ type: "command", command: "interact.agents.stop" }, COMMANDS),
    { kind: "command", command: "interact.agents.stop" });
  // Real, ours, and NOT offered by this panel — still refused. The webview renders agent output,
  // so "it exists" is not the test; "this surface offers it" is.
  assert.equal(chatAction({ type: "command", command: "interact.agents.broadcast" }, COMMANDS), null);
  assert.equal(chatAction({ type: "command", command: "workbench.action.terminal.new" }, COMMANDS), null);
});

test("opening a file passes the path, and a non-string is not a path", () => {
  assert.deepEqual(chatAction({ type: "open", path: "/w/a.ts" }, COMMANDS),
    { kind: "open", path: "/w/a.ts" });
  assert.equal(chatAction({ type: "open", path: 7 }, COMMANDS), null);
});

test("the file picker asks for nothing, so it needs no payload", () => {
  assert.deepEqual(chatAction({ type: "pickFile" }, COMMANDS), { kind: "pickFile" });
});

test("junk is refused rather than guessed at", () => {
  for (const junk of [null, undefined, 3, "send", {}, { type: "eval", code: "1" }]) {
    assert.equal(chatAction(junk, COMMANDS), null, `${JSON.stringify(junk)} produced an action`);
  }
});
