/** What you can DO with a character, decided from what that character actually is.
 *
 *  "Agent inherit all the capabilities they have through files, and they should have actions for
 *  these. And we should show they actions in a ergonomic way in the workspace."
 *
 *  Every action in the panel was global — stop, message, show events — reached from a tree context
 *  menu and offered identically whether the run was live or finished a day ago. Clicking a
 *  character in the world offered nothing at all. These are the per-character actions, and they
 *  are FILTERED by what makes sense for that one: stopping a finished agent is not an action, it
 *  is a dead menu item that teaches you the menu is not to be trusted.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { actionsFor } from "./agentActions.ts";

const worker = (over: Record<string, unknown> = {}) => ({
  run_id: "r1", name: "artist", status: "running", faculties: ["reads", "writes"],
  ...over,
}) as never;

const ids = (w: unknown) => actionsFor(w as never).map((a) => a.id);

test("a running agent can be stopped and spoken to", () => {
  const got = ids(worker());
  assert.ok(got.includes("stop"));
  assert.ok(got.includes("message"));
});

test("a finished agent offers no stop — a dead item teaches you not to trust the menu", () => {
  for (const status of ["done", "error"]) {
    assert.ok(!ids(worker({ status })).includes("stop"), `${status} still offers stop`);
  }
});

test("you cannot speak to one of your own editor sessions", () => {
  // interact does not drive it: the message would go nowhere, and offering it would promise a
  // reach the product does not have.
  const got = ids(worker({ status: "foreign" }));
  assert.ok(!got.includes("message"));
  assert.ok(!got.includes("stop"));
});

test("its transcript is always readable, whatever state it is in", () => {
  // The one thing that is true of every run, live or long finished. Transparency is the point.
  for (const status of ["running", "done", "error", "foreign"]) {
    assert.ok(ids(worker({ status })).includes("transcript"), `${status} cannot be read`);
  }
});

test("only an agent WITH a definition offers to show its prompt", () => {
  assert.ok(ids(worker({ definition_path: "/defs/artist.md" })).includes("prompt"));
  assert.ok(!ids(worker({ definition_path: null })).includes("prompt"),
    "a plain run has no definition file to open");
});

test("every action says what it does and which command runs it", () => {
  for (const action of actionsFor(worker({ definition_path: "/d.md" }) as never)) {
    assert.ok(action.label.trim(), `${action.id} has no label`);
    assert.match(action.command, /^interact\./, `${action.id} does not name an interact command`);
  }
});

test("the order is stable, so a character's menu does not reshuffle between renders", () => {
  const a = actionsFor(worker({ definition_path: "/d.md" }) as never).map((x) => x.id);
  const b = actionsFor(worker({ definition_path: "/d.md" }) as never).map((x) => x.id);
  assert.deepEqual(a, b);
});

test("the brain can be spoken to like anyone else", () => {
  // It is the orchestrator, not a special case you cannot reach — talking to it is how you steer
  // the whole team.
  assert.ok(ids(worker({ brain: true })).includes("message"));
});

test("you can choose the model from the row that IS the agent", () => {
  /* "find a way that we could easily chose what models are ran for what." Reachable where you are
     already looking at the agent, not only from a settings page elsewhere. */
  assert.ok(ids(worker({ status: "running", agent: "researcher" })).includes("model"));
});

test("a run with no definition has nothing to remember a model against", () => {
  /* The preference is stored per DEFINITION — you are choosing what `researcher` runs on, not what
     one errand runs on — so a bare session cannot offer it. */
  assert.ok(!ids(worker({ status: "running", agent: null })).includes("model"));
  assert.ok(!ids(worker({ status: "foreign", agent: "researcher" })).includes("model"),
    "and interact cannot change what one of your own editor windows runs");
});
