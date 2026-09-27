/** What each agent can actually DO, read from its own definition file.
 *
 *  "Agent inherit all the capabilities they have through files, and they should have actions for
 *  these." The definitions already declare this — `tools: [Read, Grep, Bash, mcp__interact__…]` in
 *  the frontmatter — and nothing has ever read it, so the workplace draws every character
 *  identically whether it can only read files or can drive a browser and spend money.
 *
 *  Grouped into a handful of human FACULTIES rather than listed raw: nobody wants to see
 *  `mcp__interact__get_interactive_elements` on a sprite, and a tool list is not a capability.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { parseCapabilities, facultiesOf, FACULTIES } from "./capabilities.ts";

const DEF = `---
name: visual-critic
description: "The visual authority."
model: claude-sonnet-5
tools: [Read, Grep, Glob, Bash, Agent, SendMessage, mcp__interact__screenshot, mcp__interact__run_actions]
---

Body text that mentions tools: [NotARealList] and must not be parsed.
`;

test("the declared tools are read off the frontmatter", () => {
  const tools = parseCapabilities(DEF);
  assert.ok(tools.includes("Read"));
  assert.ok(tools.includes("mcp__interact__screenshot"));
  assert.ok(!tools.includes("NotARealList"), "a tools: line in the BODY is prose, not a declaration");
});

test("a definition with no tools line yields nothing rather than guessing", () => {
  assert.deepEqual(parseCapabilities("---\nname: x\n---\nbody"), []);
  assert.deepEqual(parseCapabilities(""), []);
});

test("tools become a few human faculties, not a raw list", () => {
  // A sprite cannot wear "mcp__interact__get_interactive_elements". The point is what the
  // character can DO, in words someone watching would use.
  const f = facultiesOf(parseCapabilities(DEF));
  assert.ok(f.includes("reads"), "Read/Grep/Glob is reading");
  assert.ok(f.includes("runs"), "Bash is running things");
  assert.ok(f.includes("sees"), "the interact screenshot/actions tools are eyes and hands");
  assert.ok(f.includes("delegates"), "Agent/SendMessage is delegating");
  assert.ok(!f.includes("writes"), "this one has no Write/Edit and must not claim it");
});

test("writing is its own faculty, and is not implied by reading", () => {
  assert.ok(facultiesOf(["Read", "Write"]).includes("writes"));
  assert.ok(!facultiesOf(["Read", "Grep"]).includes("writes"));
});

test("an unknown tool never invents a faculty", () => {
  // A vendor adding a tool must not silently grant a character a power it does not have.
  assert.deepEqual(facultiesOf(["some__brand__new_tool"]), []);
});

test("every faculty a tool can map to is declared, so the world can draw them all", () => {
  const produced = new Set(
    ["Read", "Write", "Bash", "Agent", "WebSearch", "mcp__interact__screenshot"]
      .flatMap((t) => facultiesOf([t])),
  );
  for (const f of produced) {
    assert.ok(FACULTIES.some((k) => k.id === f), `${f} has no entry in FACULTIES`);
  }
});

test("each declared faculty carries a word and a mark for the world to render", () => {
  for (const f of FACULTIES) {
    assert.ok(f.label.trim(), `${f.id} has no label`);
    assert.ok(f.mark.trim(), `${f.id} has no mark`);
  }
});

test("faculties come back in a stable order, so a character does not reshuffle each render", () => {
  const a = facultiesOf(["Bash", "Read", "Agent"]);
  const b = facultiesOf(["Agent", "Bash", "Read"]);
  assert.deepEqual(a, b);
});
