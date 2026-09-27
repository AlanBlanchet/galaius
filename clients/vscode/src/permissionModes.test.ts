import { test } from "node:test";
import assert from "node:assert/strict";
import { parseModes, modeChoices, describeMode } from "./permissionModes.ts";

const OUT = [
  "plan\tPlan only\tworks out an approach and touches nothing\tno",
  "acceptEdits\tMay edit files\tfile changes go through, other actions still ask\tno",
  "bypassPermissions\tNo restrictions\tacts without asking\tyes",
].join("\n");

const MODES = parseModes(OUT);

test("every row of the CLI's output becomes a mode", () => {
  assert.deepEqual(MODES.map((m) => m.id), ["plan", "acceptEdits", "bypassPermissions"]);
  assert.equal(MODES[0].label, "Plan only");
  assert.equal(MODES[0].detail, "works out an approach and touches nothing");
});

test("the unrestricted flag is read from the column, not inferred from the wording", () => {
  assert.deepEqual(MODES.filter((m) => m.unrestricted).map((m) => m.id), ["bypassPermissions"]);
});

test("a line that is not a mode is dropped rather than offered as one", () => {
  // The realistic case: the CLI is missing or errors, and stderr lands here.
  assert.deepEqual(parseModes("ERROR: no such provider 'nope'"), []);
  assert.deepEqual(parseModes(""), []);
  assert.deepEqual(parseModes("plan\tPlan only"), [], "a truncated row is not half a mode");
});

test("the CLI's own default leads, so dismissing the picker changes nothing", () => {
  const choices = modeChoices(MODES);
  assert.equal(choices[0].id, null);
  assert.match(choices[0].detail, /already configured/);
});

test("the last choice floats to the top so repeat spawns are one keypress", () => {
  const choices = modeChoices(MODES, "plan");
  assert.equal(choices[0].id, "plan");
  assert.equal(choices.length, MODES.length + 1, "floating must not duplicate the entry");
  assert.equal(new Set(choices.map((c) => c.id)).size, choices.length);
});

test("a remembered mode the provider no longer offers is ignored, not resurrected", () => {
  const choices = modeChoices(MODES, "modeThatWasRemoved");
  assert.equal(choices[0].id, null);
  assert.equal(choices.length, MODES.length + 1);
});

test("the unrestricted mode is marked in its own label", () => {
  const bypass = modeChoices(MODES).find((c) => c.id === "bypassPermissions")!;
  assert.match(bypass.label, /warning/);
  assert.match(bypass.detail, /no confirmation/);
  const plan = modeChoices(MODES).find((c) => c.id === "plan")!;
  assert.doesNotMatch(plan.label, /warning/, "only the dangerous one is marked");
});

test("a run with no recorded mode says nothing rather than claiming a default", () => {
  assert.equal(describeMode(null, MODES), null);
  assert.equal(describeMode(undefined, MODES), null);
});

test("a recorded mode is described in words, carrying its danger flag", () => {
  assert.deepEqual(describeMode("plan", MODES), { label: "Plan only", unrestricted: false });
  assert.deepEqual(describeMode("bypassPermissions", MODES),
    { label: "No restrictions", unrestricted: true });
});

test("a mode this build does not know is shown verbatim, and fails CLOSED", () => {
  // A run really was started with it, so hiding it would misreport the autonomy an agent runs
  // under. Marked unrestricted because a newer CLI's MORE permissive mode must not render as an
  // unremarkable one just because this build has not heard of it.
  assert.deepEqual(describeMode("somethingNewer", MODES),
    { label: "somethingNewer", unrestricted: true });
});
