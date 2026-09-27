/** How the secondary-sidebar panel divides itself at REST.
 *
 *  His ask, verbatim: "The chat page shouldn't be small." An independent visual-critic round
 *  measured it at ~300px on cold entry — a third of a narrow sidebar, because three views stack
 *  in one container and the middle one was a second tree listing the same roster the rail above
 *  it already shows.
 *
 *  Layout that only exists in a manifest is exactly what regresses silently: nothing imports it,
 *  no compiler checks it, and the symptom (a cramped conversation) looks like a styling opinion
 *  rather than a dropped decision. This pins it.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/** Every command the manifest declares. A command hidden by a `when: false` palette entry and one
 *  removed from the manifest altogether are both off the palette. */
const declared = (manifest: { contributes: { commands: { command: string }[] } }) =>
  new Set(manifest.contributes.commands.map((c) => c.command));


type View = { id: string; name?: string; initialSize?: number; visibility?: string; when?: string };

const views: View[] = JSON.parse(
  readFileSync(join(import.meta.dirname, "..", "package.json"), "utf8"),
).contributes.views.interactAgentsSecondary;

const byId = (id: string): View => {
  const v = views.find((x) => x.id === id);
  assert.ok(v, `${id} is gone from the panel entirely`);
  return v;
};

test("the side bar holds the conversation and nothing else", () => {
  /* His words, after several rounds of me rearranging the wrong container: "Your team and agents
     panel are still on the left side, whereas they should be in the big main panel somewhere...
     That's what i've been trying to make you understand but without telling you."

     The room and the roster are two views of ONE company and belong together in the wide editor
     panel. The side bar is for the single conversation you are having — "the sidepanel is there to
     view info about who we click on, and view the conversation. That's all." Stacking the roster
     in there was the mistake underneath every earlier squeeze-the-column fix. */
  assert.deepEqual(views.map((v) => v.id), ["interactAgents.chat"],
    "anything else in this container is the roster creeping back into the side bar");
});

test("the conversation needs no when-clause, being alone", () => {
  /* It used to hide behind `interact.inConversation` so the roster could have the column back.
     With the roster gone to the main panel there is nothing to trade the column with — and a
     `when` clause is what made VS Code dispose this view and blank the sidebar on the second open. */
  assert.equal(byId("interactAgents.chat").when, undefined);
});

test("every view still declares a size, so none silently collapses to nothing", () => {
  for (const v of views) {
    assert.ok(
      typeof v.initialSize === "number" && v.initialSize > 0,
      `${v.id} has no initialSize — its height then depends on declaration order`,
    );
  }
});

test("a view that VS Code can dispose is never written to afterwards", () => {
  /* The `when` clause that gives the conversation the whole column also makes VS Code DISPOSE the
     chat view every time the clause goes false. A kept reference to a disposed webview throws on
     the next write — which blanked the entire sidebar on the SECOND conversation a user opened,
     with no visible way back.

     Not unit-testable: chatView.ts imports `vscode`, which the strip-types loader cannot resolve,
     and that is exactly why the defect reached a live host. So the contract is pinned at the
     source: forget the view on disposal, and never paint one that is not visible. The roster view
     has always done both; this file only became disposable when it gained a `when`. */
  const chat = readFileSync(join(import.meta.dirname, "chatView.ts"), "utf8");
  assert.match(chat, /onDidDispose\(/,
    "chatView must forget a disposed view, or the next show() writes to a dead webview");
  assert.match(chat, /this\.view\s*=\s*undefined/,
    "onDidDispose must actually clear the reference, not merely log");
  assert.match(chat, /if \(!this\.view\?\.visible\) return;/,
    "render() must refuse a hidden view — writing to one VS Code tore down throws, it does not no-op");

  /* Kept even though no view carries a `when` today: VS Code disposes a webview view whenever it
     hides, and this file cost a blanked sidebar once already. The guard is cheap; rediscovering it
     is not. */
});

test("you cannot prompt an agent from the command palette", () => {
  /* "Remove from everywhere the fact that we can prompt from the vscode CTRL+P box at the top.
     Everything should be in the dashboard."

     Giving an agent work is the panel's job. A palette entry that opens an input box is a second,
     hidden way in — with none of the context the panel has about who is free, what they cost, or
     what they are already doing. */
  const manifest = JSON.parse(readFileSync(join(import.meta.dirname, "..", "package.json"), "utf8"));
  const hidden = new Set(
    (manifest.contributes.menus?.commandPalette ?? [])
      .filter((e: { when?: string }) => e.when === "false")
      .map((e: { command: string }) => e.command),
  );
  for (const cmd of ["interact.agents.spawn", "interact.agents.send", "interact.agents.broadcast"]) {
    assert.ok(hidden.has(cmd), `${cmd} prompts, so it must not be reachable from the palette`);
  }
});

test("no command that needs a subject is offered where it has none", () => {
  /* These take a {run}; invoked from the palette they silently do nothing, which teaches you the
     whole list is untrustworthy. */
  const manifest = JSON.parse(readFileSync(join(import.meta.dirname, "..", "package.json"), "utf8"));
  const hidden = new Set(
    (manifest.contributes.menus?.commandPalette ?? [])
      .filter((e: { when?: string }) => e.when === "false")
      .map((e: { command: string }) => e.command),
  );
  for (const cmd of ["interact.agents.stop", "interact.agents.showEvents",
                     "interact.agents.openConversation"]) {
    assert.ok(hidden.has(cmd), `${cmd} needs a subject and would no-op from the palette`);
  }
});

test("EVERY command that opens a prompt is hidden from the palette, not just the ones we listed", () => {
  /* "Remove from everywhere the fact that we can prompt from the vscode CTRL+P box at the top.
     Everything should be in the dashboard."

     Two earlier tests name three commands each, by hand. `interact.agents.newSession` was added
     later, was in neither list, and shipped in the palette opening "What do you want done?" — the
     exact box he asked to be rid of. A hand-kept list cannot cover a command nobody remembered to
     add to it, so this derives the list from the SOURCE: anything that calls `showInputBox` or
     `showQuickPick` must be hidden. */
  const manifest = JSON.parse(readFileSync(join(import.meta.dirname, "..", "package.json"), "utf8"));
  const source = readFileSync(join(import.meta.dirname, "extension.ts"), "utf8");
  const hidden = new Set(
    (manifest.contributes.menus?.commandPalette ?? [])
      .filter((e: { when?: string }) => e.when === "false")
      .map((e: { command: string }) => e.command),
  );

  // FREE TEXT is the subject of his decision — "prompt from the box". A quick pick offers a
  // choice; an input box asks you to type, which is the thing that must live in the dashboard.
  const prompts: string[] = [];
  const registration = /registerCommand\(\s*"([^"]+)"([\s\S]*?)(?=\n    vscode\.commands\.registerCommand\(|\n  \);)/g;
  for (const [, command, body] of source.matchAll(registration)) {
    if (/showInputBox/.test(body)) prompts.push(command);
  }
  assert.ok(prompts.length >= 3, `expected to find prompting commands, found ${prompts.length}`);

  const exposed = prompts.filter((c) => declared(manifest).has(c) && !hidden.has(c));
  assert.deepEqual(exposed, [],
    `these open a box from Ctrl+Shift+P: ${exposed.join(", ")} — give them a dashboard entry instead`);
});
