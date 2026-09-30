/** The Agents sidebar's pure logic, tested without VS Code.
 *
 *  The tree itself needs the `vscode` module (only available inside the extension host), so the
 *  grouping / labelling / formatting decisions live as exported pure functions and are pinned
 *  here. Run with: node --experimental-strip-types --test src/agentsView.test.ts
 *
 *  These exist because the panel's whole job is to tell the truth at a glance: an unknown cost
 *  rendered as "$0.00" claims a run was free, and a group that hides live work defeats the point
 *  of a sidebar.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentRun } from "./agents.ts";
import {
  formatCost,
  formatElapsed,
  groupKeyFor,
  orderGroups,
  rowDescription,
  statusIcon,
} from "./agentsFormat.ts";
import { runTooltip } from "./billingPresentation.ts";

function run(over: Partial<AgentRun> = {}): AgentRun {
  return {
    run_id: "r1",
    provider: "claude",
    name: "worker",
    status: "running",
    cwd: "/tmp/project/dev/galaius",
    started_at: 1_000,
    ...over,
  } as AgentRun;
}

test("unknown cost is an em dash, never $0.00", () => {
  assert.equal(formatCost(null), "—");
  assert.equal(formatCost(undefined), "—");
  assert.equal(formatCost(0.6155), "~$0.6155");
});

test("a run tooltip reports its billing path without inventing account coverage", () => {
  const unknown = runTooltip(run({
    charge_path: "unknown", cost_certainty: "unknown", cost_usd: null,
  }));
  assert.match(unknown, /Unknown charge path and account impact/i);
  assert.doesNotMatch(unknown, /already paid|\bfree\b|\$0(?:\.0+)?(?![\d.])/i);
  assert.match(runTooltip(run({
    charge_path: "subscription_quota", cost_certainty: "unknown", cost_usd: 2.4,
  })), /~\$2\.4000 API-equivalent usage.*Subscription quota and account impact not reported/i);
  assert.match(runTooltip(run({
    charge_path: "metered_api", cost_certainty: "known", cost_usd: 0.32,
  })), /\$0\.3200 observed metered API charge/i);
});

test("a project group is the directory's basename, not the whole path", () => {
  assert.equal(groupKeyFor(run(), "project"), "galaius");
  assert.equal(groupKeyFor(run({ cwd: "" }), "project"), "(no project)");
});

test("provider and model groups fall back to something readable", () => {
  assert.equal(groupKeyFor(run(), "provider"), "claude");
  assert.equal(groupKeyFor(run({ model: null }), "model"), "(default model)");
  assert.equal(groupKeyFor(run({ model: "claude-opus-5" }), "model"), "claude-opus-5");
});

test("groups with live work sort first", () => {
  // A sidebar you must scroll to find the running agent is a sidebar that failed.
  const idle: [string, AgentRun[]] = ["zzz-idle", [run({ status: "done" })]];
  const live: [string, AgentRun[]] = ["aaa-live", [run({ status: "running" })]];
  assert.deepEqual(orderGroups([idle, live]).map(([k]) => k), ["aaa-live", "zzz-idle"]);
});

test("elapsed counts to now while running, and freezes once finished", () => {
  const started = Date.now() / 1000 - 90;
  assert.equal(formatElapsed(run({ started_at: started })), "1m");
  assert.equal(
    formatElapsed(run({ started_at: 1_000, finished_at: 1_030, status: "done" })),
    "30s",
  );
});

test("a run groups by its repo, not the subfolder it happened to run in", () => {
  // The whole point: an agent working in `<repo>/src` belongs to the repo, not to a "src" group.
  const inSub = run({ cwd: "/tmp/project/dev/galaius/src", project: "galaius" }) as never;
  assert.equal(groupKeyFor(inSub, "project"), "galaius");
});

test("a record written before projects existed still groups sensibly", () => {
  const old = run({ cwd: "/tmp/project/dev/galaius", project: undefined }) as never;
  assert.equal(groupKeyFor(old, "project"), "galaius");
});

// The panel is narrow, and every row read "→ 2b7642ee: Reply wit…", "thanks, ship it :…" — the
// interesting part clipped by metadata that is already in the tooltip AND on the dashboard.
// The row's job is to say what the agent is DOING; the numbers can wait for the hover.


const base = { run_id: "r1", name: "reviewer", provider: "claude", status: "done" } as any;

test("a row leads with what happened, not with the numbers", () => {
  const d = rowDescription({ ...base, last: "← operator: check the error paths", cost_usd: 1.2 });
  assert.ok(d.startsWith("← operator:"), d);
});

test("the numbers are dropped when there is something to say", () => {
  const d = rowDescription({ ...base, last: "← operator: check the error paths", cost_usd: 1.2 });
  assert.ok(!d.includes("$"), `cost competes for width with the message: ${d}`);
});

test("with nothing to say the row falls back to status and cost", () => {
  const d = rowDescription({ ...base, last: "", cost_usd: 1.2 });
  assert.match(d, /done/);
  assert.match(d, /\$1\.2/);
});

test("a long message is clipped at a word, not mid-word", () => {
  const long = "← operator: " + "verylongword ".repeat(20);
  const d = rowDescription({ ...base, last: long, cost_usd: null });
  assert.ok(d.length <= 80, `too long: ${d.length}`);
  assert.ok(!/verylongwo…$/.test(d), `clipped mid-word: ${d}`);
  assert.match(d, /…$/);
});

test("a short message is left exactly as it is", () => {
  assert.equal(rowDescription({ ...base, last: "done", cost_usd: null }), "done");
});

// A session galaius did not start reports a MODE, not an activity: `last` is "interactive" or
// "background". Rendered through the normal path, its row read "interactive" where a team member's
// reads "delegating the workplace view…" — a different KIND of word in the same column, which the
// eye parses as an activity that never happened.
test("a foreign session's row says whose it is, not its transport mode", () => {
  const row = rowDescription({ last: "interactive", status: "foreign", foreign: true });
  assert.doesNotMatch(row, /^interactive$/, "a mode label is not an activity");
  assert.match(row, /your own|not started by galaius/i);
});

test("a foreign session that IS doing something recognisable still says so", () => {
  // Only the two known transport labels are replaced; anything else is real content.
  const row = rowDescription({ last: "editing src/main.rs", status: "foreign", foreign: true });
  assert.equal(row, "editing src/main.rs");
});

test("a team member's row is untouched by the foreign rule", () => {
  assert.equal(rowDescription({ last: "interactive", status: "running" }), "interactive");
});

// The icon is what most rows are read by: the panel exists so state is obvious from the corner of
// the eye. `foreign` and `stopped` shared one colour token and differed by glyph alone, so a LIVE
// session of your own read as the same tier as a dead run. Untestable until now — the map lived in
// agentsView.ts, which imports `vscode` and so cannot be loaded here at all.
test("a foreign session does not wear the same colour as a stopped one", () => {
  assert.notEqual(statusIcon("foreign").color, statusIcon("stopped").color,
    "foreign is about ownership, not about being finished");
});

test("every status has an icon, and an unknown one falls back rather than blanking", () => {
  for (const s of ["running", "done", "failed", "interrupted", "stopped", "foreign"]) {
    assert.ok(statusIcon(s).id, `${s} has no icon`);
  }
  assert.ok(statusIcon("something-new").id, "an unknown status must still render something");
});

test("only a running agent spins", () => {
  // A spinner on anything else claims work is happening when it is not.
  assert.match(statusIcon("running").id, /~spin$/);
  for (const s of ["done", "failed", "interrupted", "stopped", "foreign"]) {
    assert.doesNotMatch(statusIcon(s).id, /~spin$/, `${s} spins`);
  }
});
