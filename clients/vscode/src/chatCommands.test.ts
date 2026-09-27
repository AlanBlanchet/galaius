/** What you can DO from the chat panel, not just say.
 *
 *  His ask: "you should ship every feature of claude code, codex, and copilot inside the interact
 *  chat panel such that i can control everything from there." Today the panel can send a string.
 *  Everything else — stopping the agent you are reading, starting another, switching workspace,
 *  opening the team — lives in a tree context menu or the command palette, which is precisely
 *  "not being able to control everything from there".
 *
 *  A slash menu is how all three reference tools expose this, so the commands are data: one list
 *  drives the menu, the typed-slash completion, and the buttons.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { CHAT_COMMANDS, matchCommands, commandFor } from "./chatCommands.ts";

test("every command names the extension command it actually invokes", () => {
  // A menu entry wired to nothing is the unreachable-capability defect wearing a different hat.
  for (const c of CHAT_COMMANDS) {
    assert.match(c.command, /^interact\./, `${c.slash} invokes ${c.command}`);
    assert.ok(c.title.length > 0, `${c.slash} has no title`);
  }
});

test("slash names are unique — a menu with two /stops is a coin flip", () => {
  const slashes = CHAT_COMMANDS.map((c) => c.slash);
  assert.equal(new Set(slashes).size, slashes.length);
});

test("typing a prefix narrows the menu", () => {
  const hits = matchCommands("/st").map((c) => c.slash);
  assert.ok(hits.includes("/stop"), `expected /stop in ${hits.join(", ")}`);
  assert.ok(!hits.includes("/team"));
});

test("an empty slash offers everything, in declared order", () => {
  assert.deepEqual(matchCommands("/").map((c) => c.slash), CHAT_COMMANDS.map((c) => c.slash));
});

test("plain text is not a command — the composer is still a message box", () => {
  assert.deepEqual(matchCommands("stop the agent"), []);
  assert.deepEqual(matchCommands(""), []);
});

test("a command is recognised only when it is the WHOLE message", () => {
  // "/stop" sends the command; "/stop please also do X" is a sentence that happens to start with
  // a slash, and silently swallowing it would lose what the person actually typed.
  assert.equal(commandFor("/stop")?.slash, "/stop");
  assert.equal(commandFor("  /stop  ")?.slash, "/stop", "surrounding whitespace is not meaningful");
  assert.equal(commandFor("/stop and then rerun"), undefined);
  assert.equal(commandFor("hello"), undefined);
});

test("the ones that only make sense with an agent selected are marked", () => {
  const stop = CHAT_COMMANDS.find((c) => c.slash === "/stop")!;
  const team = CHAT_COMMANDS.find((c) => c.slash === "/team")!;
  assert.equal(stop.needsAgent, true, "stopping needs something to stop");
  assert.equal(team.needsAgent, false);
});

test("the commands cover what he cannot do from the panel today", () => {
  const slashes = CHAT_COMMANDS.map((c) => c.slash);
  for (const expected of ["/stop", "/new", "/agent", "/team", "/workspace", "/sequence", "/prompt"]) {
    assert.ok(slashes.includes(expected), `missing ${expected}`);
  }
});

test("broadcasting is offered, and is not something you can do by accident", () => {
  const all = CHAT_COMMANDS.find((c) => c.slash === "/all");
  assert.ok(all, "no way to address the whole team from the panel");
  assert.equal(all!.needsAgent, false, "the team exists whether or not one agent is open");
  assert.match(all!.detail, /running/i, "it must say WHO it reaches before you use it");
});
