/** A capability nobody can reach is not a capability.
 *
 *  Three times in one arc a feature was written, tested, and left unwired — the workspace scope
 *  most recently, which existed as two well-tested modules bound to no command and therefore
 *  invisible to the person who asked for it. A unit test on the logic passes happily in that
 *  state, which is exactly why it needs its own check: every command the extension registers must
 *  be declared in the manifest, and every command the manifest declares must be registered.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8"));
const source = readFileSync(join(here, "extension.ts"), "utf8");
const require = createRequire(import.meta.url);

const declared: string[] = (manifest.contributes.commands ?? []).map((c: { command: string }) => c.command);
const registered = [...source.matchAll(/registerCommand\(\s*"([^"]+)"/g)].map((m) => m[1]);

test("every command the manifest advertises is actually implemented", () => {
  const missing = declared.filter((c) => !registered.includes(c));
  assert.deepEqual(missing, [], "advertised in the command palette, does nothing when invoked");
});

/** Commands invoked only in code, never by a person.
 *
 *  Each takes an argument that a palette entry could not supply — `interact.agents.chat` needs a
 *  run id — so declaring them would put a broken row in the command palette. Listed explicitly so
 *  that "internal" is a decision someone made, not a manifest entry someone forgot.
 */
const INTERNAL = ["interact.agents.chat"];

test("every command the extension implements is reachable, or explicitly internal", () => {
  const hidden = registered.filter((c) => !declared.includes(c) && !INTERNAL.includes(c));
  assert.deepEqual(hidden, [], "implemented but unreachable — nobody can invoke it");
});

test("the internal list does not hide something that IS declared", () => {
  // Otherwise the exemption quietly grows into a place to park things.
  assert.deepEqual(INTERNAL.filter((c) => declared.includes(c)), []);
});

test("the workspace switcher is reachable from the panel's title bar", () => {
  // The specific one he could not reach: "i can't change the workspace... i have agents in the
  // 'sheets' folder elsewhere, and i can't change and see how they work."
  const menus = manifest.contributes.menus["view/title"] ?? [];
  const entry = menus.find((m: { command: string }) => m.command === "interact.agents.workspace");
  assert.ok(entry, "no title-bar button, so the only way in is the command palette");
  assert.match(entry.when, /interactAgents\.board/);
});

test("every slash command in the chat invokes a command that exists", () => {
  // The chat menu is a second place a capability can be advertised and wired to nothing.
  const { CHAT_COMMANDS } = require("../out/chatCommands.js");
  const missing = CHAT_COMMANDS
    .map((c: { slash: string; command: string }) => c)
    .filter((c: { command: string }) => !registered.includes(c.command))
    .map((c: { slash: string; command: string }) => `${c.slash} -> ${c.command}`);
  assert.deepEqual(missing, [], "advertised in the chat menu, invokes nothing");
});

/** A module imported and never CALLED was the recurring defect here — `teamSpend` shipped a whole
 *  commit imported into the chat view and never invoked, so the row rendered in the preview
 *  fixture (which supplies its own spend) and never once in the panel.
 *
 *  The first fix was a hand-kept CONSUMERS list, which is the same shape as the file list the CSS
 *  checker abandoned for discovery in this very diff — and whose failure message read "update this
 *  list". The compiler already knows: `noUnusedLocals` in tsconfig.json fails the build on an
 *  import nothing references, and it found NINE dead ones the hand-list never mentioned, including
 *  an `execFile` left behind exactly as this defect leaves them. This test pins the SETTING, so a
 *  later tsconfig edit cannot quietly remove the guard.
 */
test("the build refuses an import nothing uses", () => {
  const tsconfig = readFileSync(join(here, "..", "tsconfig.json"), "utf8");
  // Read as text rather than JSON.parse: the file carries comments explaining WHY this is on, and
  // stripping them to parse would be more machinery than the check is worth.
  assert.match(tsconfig, /"noUnusedLocals"\s*:\s*true/,
    "noUnusedLocals is what catches a feature imported into a view and never wired up");
});

/** A command drawn with an icon was meant to be a BUTTON. Declared with an icon yet placed in no
 *  menu, slash command or rail action, it can be reached only by typing its name into the palette
 *  — the unwired fingerprint this file exists for. The providers switch shipped in 0.39.0 exactly
 *  so: `$(plug)`, nowhere to click. */
test("every command drawn with an icon is placed where a person can click it", () => {
  const menus: Record<string, { command: string }[]> = manifest.contributes.menus ?? {};
  const placed = new Set(
    Object.entries(menus).filter(([where]) => where !== "commandPalette").flatMap(([, v]) => v.map((m) => m.command)),
  );
  // The chat's slash menu and the rail's action row are the two other places a command is wired
  // to something a person clicks or types.
  for (const file of ["chatCommands.ts", "rail.ts"]) {
    const src = readFileSync(join(here, file), "utf8");
    for (const m of src.matchAll(/command:\s*"([^"]+)"/g)) placed.add(m[1]);
  }
  const unplaced = (manifest.contributes.commands as { command: string; icon?: string }[])
    .filter((c) => c.icon && !placed.has(c.command))
    .map((c) => c.command);
  assert.deepEqual(unplaced, [], "drawn with an icon, reachable only by typing its name");
});
