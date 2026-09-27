/** The rail's decisions: what the panel shows at rest, and in what order.
 *
 *  Why a rail at all is measured rather than stylistic. VS Code hides a view's title actions until
 *  the pointer enters the header and CLIPS the overflow with no "…" menu, so the panel rendered
 *  ZERO buttons at rest and the team view could only be found by hovering and guessing among
 *  unlabelled glyphs. No arrangement of icons fixes that inside a `TreeView` at any width.
 *
 *  The two decisions worth pinning are the ones that make this a supervision surface rather than
 *  a list: the roster is ordered by WHO NEEDS YOU, and the destinations carry words.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { attentionOf, brainOf, buildRail, railAction, railRoute, CHIPS, HELD_SECONDS, RANK, SCOPE_COMMAND } from "./rail.ts";
import { buildTeam } from "./teamState.ts";
import { actionsFor } from "./agentActions.ts";

const run = (over: Record<string, unknown> = {}) => ({
  run_id: "r", name: "worker", provider: "claude", status: "running", started_at: 100, ...over,
}) as never;

const rail = (runs: unknown[], idle: (r: never) => number = () => 0, asked = () => false) =>
  buildRail(runs as never[], "interact", idle as never, asked as never);

test("a run that failed outranks everything else", () => {
  const built = rail([
    run({ run_id: "ok", status: "running" }),
    run({ run_id: "done", status: "done" }),
    run({ run_id: "bad", status: "crashed" }),
  ]);
  assert.equal(built.runs[0].run.run_id, "bad");
  assert.equal(built.runs[0].attention, "error");
});

test("a running agent that has said nothing for two minutes is asking for attention", () => {
  assert.equal(attentionOf(run(), HELD_SECONDS - 1), "working");
  assert.equal(attentionOf(run(), HELD_SECONDS), "held");
});

test("a run that asked you something outranks one merely sitting quiet", () => {
  const built = rail(
    [run({ run_id: "quiet" }), run({ run_id: "asked" })],
    (r) => (r.run_id === "quiet" ? 9999 : 0),
    (r) => r.run_id === "asked",
  );
  assert.deepEqual(built.runs.map((r) => r.run.run_id), ["asked", "quiet"]);
});

test("your own sessions stand in their own band, never interleaved with the team", () => {
  /* Two of his pronouncements meet here. "We have agents that are greyed out... i still don't
     have only the conversations or things i interact with" — so they must NOT sit as grey dead
     rows inside the team's list (they were dropped entirely for a while). But "I don't have a
     view just like in claude code... of agent building things" — dropping them hid the actual
     company: the rail led with dead smoke probes while six real sessions built his projects
     unseen. The reconciliation: a separate band, titled by project, each row opening its
     transcript — visible, actionable, and out of the team's way. */
  const built = rail([run({ run_id: "mine", status: "foreign" }), run({ run_id: "bad", status: "failed" })]);
  assert.deepEqual(built.runs.map((r) => r.run.run_id), ["bad"]);
  assert.deepEqual(built.yours?.map((r) => r.run.run_id), ["mine"]);
});

test("within one band the most recently started leads", () => {
  const built = rail([
    run({ run_id: "old", status: "failed", started_at: 10 }),
    run({ run_id: "new", status: "failed", started_at: 900 }),
  ]);
  assert.deepEqual(built.runs.map((r) => r.run.run_id), ["new", "old"]);
});

test("the header counts what NEEDS you, not what is merely running", () => {
  // "8 running" answers "is it busy", which is never the question a supervisor is asking.
  const built = rail(
    [run({ run_id: "a" }), run({ run_id: "b" }), run({ run_id: "c", status: "crashed" })],
  );
  assert.equal(built.header.needsYou, 1);
  assert.equal(built.header.working, 2);
});

test("every destination carries a word, and each invokes a real command", () => {
  for (const chip of CHIPS) {
    assert.ok(chip.label.trim(), `${chip.id} has no label — an icon you must hover is a guess`);
    assert.match(chip.command, /^interact\./);
  }
  assert.ok(CHIPS.some((c) => c.id === "team"), "the team must be a first-class destination");
});

test("the destinations stay few enough to fit a narrow sidebar", () => {
  // The old title bar carried seven actions and clipped the seventh at every width. The fix is
  // fewer destinations with words, not smaller glyphs.
  assert.ok(CHIPS.length <= 4, `${CHIPS.length} chips will not fit at 292px`);
});

// The webview renders agent output, so anything arriving FROM it is untrusted. Executing a
// command id because a message said so would be a real hole — the chat view already validates
// against its declared list, and the rail must not be the softer sibling.
test("only a command the rail actually offers is accepted", () => {
  assert.deepEqual(railAction({ type: "command", command: "interact.agents.team" }),
    { kind: "command", command: "interact.agents.team" });
  assert.equal(railAction({ type: "command", command: "workbench.action.terminal.new" }), null,
    "a command the rail does not offer must be refused, however real it is");
  assert.equal(railAction({ type: "command", command: "interact.agents.stop" }), null,
    "even one of OUR commands is refused unless the rail offers it");
});

test("opening a run passes the id through, and nothing else", () => {
  assert.deepEqual(railAction({ type: "open", runId: "abc" }), { kind: "open", runId: "abc" });
  assert.equal(railAction({ type: "open" }), null);
  assert.equal(railAction({ type: "open", runId: 42 }), null, "a non-string id is not an id");
});

test("junk is refused rather than guessed at", () => {
  for (const junk of [null, undefined, 0, "open", { type: "nope" }, {}]) {
    assert.equal(railAction(junk), null, `${JSON.stringify(junk)} produced an action`);
  }
});

test("a chip the rail offers runs its command", () => {
  const done: string[] = [];
  railRoute({ type: "command", command: "interact.agents.team" }, {
    run: (c) => done.push(c), open: () => {}, act: () => {},
  });
  assert.deepEqual(done, ["interact.agents.team"]);
});

test("a command the rail does NOT offer runs nothing", () => {
  // The rail renders agent output. A postMessage naming any command id would be a real hole, so
  // "is it a real command" is not the test — "does this surface offer it" is.
  const done: string[] = [];
  for (const command of ["interact.agents.stop", "workbench.action.terminal.new", ""]) {
    railRoute({ type: "command", command }, { run: (c) => done.push(c), open: () => {}, act: () => {} });
  }
  assert.deepEqual(done, []);
});

test("clicking a row opens that run", () => {
  const opened: string[] = [];
  railRoute({ type: "open", runId: "abc" }, { run: () => {}, open: (id) => opened.push(id), act: () => {} });
  assert.deepEqual(opened, ["abc"]);
});

test("junk does nothing at all", () => {
  let touched = false;
  const mark = () => { touched = true; };
  for (const junk of [null, undefined, 0, "open", {}, { type: "eval", code: "1" }]) {
    railRoute(junk, { run: mark, open: mark, act: mark });
  }
  assert.equal(touched, false);
});

// The rail cannot replace the tree until it shows the COMPANY, not just a flat list: who sent
// whom, and who is at the head of it. "A lot of transparency" is that — a roster where a
// sub-agent floats loose beside its lead tells you nothing about who is driving what.
test("the brain is MARKED, and never displaces what needs you", () => {
  // Pinning it to the top was tried and reverted: this surface sorts by who needs you, and a
  // healthy orchestrator burying a crashed agent is exactly the sort the rail replaced.
  const built = buildRail([
    run({ run_id: "boss", status: "running", started_at: 100 }),
    run({ run_id: "worker", status: "failed", started_at: 900 }),
  ], "x", () => 0);
  assert.equal(built.runs[0].run.run_id, "worker", "the crash still leads");
  assert.equal(built.runs.find((r) => r.run.run_id === "boss")?.brain, true);
  assert.equal(built.runs.find((r) => r.run.run_id === "worker")?.brain, false);
});

test("a report sits under its lead, not loose beside it", () => {
  const built = buildRail([
    run({ run_id: "boss", status: "running", started_at: 100 }),
    run({ run_id: "kid", status: "running", parent_run_id: "boss", started_at: 200 }),
    run({ run_id: "other", status: "running", started_at: 300 }),
  ], "x", () => 0);
  // Adjacency is the claim, not position: leads are still ordered by who needs you, so which
  // lead comes first depends on their state. What must hold is that a report is never separated
  // from the lead that sent it.
  const order = built.runs.map((r) => `${r.run.run_id}@${r.depth}`);
  const bossAt = order.indexOf("boss@0");
  assert.ok(bossAt >= 0, `boss missing from ${order.join(" ")}`);
  assert.equal(order[bossAt + 1], "kid@1", "the report must follow its lead immediately");
});

test("a report whose lead is gone is a lead again, never lost", () => {
  // Its parent finished and was cleared. Hiding it would drop a live agent off the roster.
  const built = buildRail([
    run({ run_id: "orphan", status: "running", parent_run_id: "vanished" }),
  ], "x", () => 0);
  assert.equal(built.runs.length, 1);
  assert.equal(built.runs[0].depth, 0);
});

test("within one lead, its reports keep the attention order", () => {
  const built = buildRail([
    run({ run_id: "boss", status: "running", started_at: 100 }),
    run({ run_id: "fine", status: "running", parent_run_id: "boss", started_at: 200 }),
    run({ run_id: "broken", status: "crashed", parent_run_id: "boss", started_at: 300 }),
  ], "x", () => 0);
  assert.deepEqual(built.runs.map((r) => r.run.run_id), ["boss", "broken", "fine"]);
});

test("the rail and the workplace agree on who the brain is", () => {
  // The rule is duplicated because neither module can import the other under the test loader.
  // That is defensible only while the two answers are identical, so this pins it — if they ever
  // diverge, the panel and the building would crown different agents.
  const cases: unknown[][] = [
    [run({ run_id: "a", started_at: 100 }), run({ run_id: "b", parent_run_id: "a", started_at: 200 })],
    [run({ run_id: "late", started_at: 900 }), run({ run_id: "early", started_at: 100 })],
    [run({ run_id: "mine", status: "foreign", started_at: 1 }), run({ run_id: "ours", started_at: 500 })],
    [run({ run_id: "orphan", parent_run_id: "gone", started_at: 50 })],
    [],
  ];
  for (const runs of cases) {
    const fromRail = brainOf(runs as never[]);
    const fromWorld = buildTeam(runs as never[], () => [], 2000).workers.find((w) => w.brain);
    assert.equal(fromRail, fromWorld?.run_id ?? null,
      `disagreement on ${JSON.stringify(runs.map((r) => (r as { run_id: string }).run_id))}`);
  }
});

test("a row action names both the command and the agent it acts on", () => {
  const acted: string[] = [];
  railRoute({ type: "act", command: "interact.agents.stop", runId: "r7" },
    { run: () => {}, open: () => {}, act: (c, id) => acted.push(`${c}@${id}`) });
  assert.deepEqual(acted, ["interact.agents.stop@r7"]);
});

test("a row action with no agent, or an unoffered command, does nothing", () => {
  const acted: string[] = [];
  const h = { run: () => {}, open: () => {}, act: (c: string, id: string) => acted.push(c + id) };
  railRoute({ type: "act", command: "interact.agents.stop" }, h);
  railRoute({ type: "act", command: "workbench.action.terminal.new", runId: "r7" }, h);
  railRoute({ type: "act", command: "interact.agents.broadcast", runId: "r7" }, h);
  assert.deepEqual(acted, []);
});

test("every action a row can show is one the rail will accept", () => {
  // The allowlist and the buttons live in modules that cannot import each other. If they drift,
  // a visible control silently does nothing — so this pins them together.
  for (const status of ["running", "done", "error", "foreign"]) {
    for (const a of actionsFor({ run_id: "r", status, definition_path: "/d.md" } as never)) {
      const routed: string[] = [];
      railRoute({ type: "act", command: a.command, runId: "r" },
        { run: () => {}, open: () => {}, act: (c) => routed.push(c) });
      assert.deepEqual(routed, [a.command], `${status}/${a.id} is shown but refused`);
    }
  }
});

test("the scope label can change the scope, without becoming a destination", () => {
  /* "i can't switch to other projects from the panel." The command existed but only in the palette.
     It lives on the scope pill — the thing that STATES which project you are looking at — rather
     than as a fifth chip, because the chips are capped to what fits a narrow sidebar. */
  assert.deepEqual(
    railAction({ type: "command", command: SCOPE_COMMAND }),
    { kind: "command", command: SCOPE_COMMAND },
  );
  assert.ok(!CHIPS.some((c) => c.command === SCOPE_COMMAND), "it must not take a chip slot");
  assert.equal(railAction({ type: "command", command: "interact.somethingElse" }), null,
    "and nothing else gets in through the same door");
});

test("going into an agent narrows the roster to its conversations", () => {
  /* "When i click on an agent, i should be able to view the conversations is had." An agent is a
     ROLE; the conversations are the engagements. Filtering happens BEFORE counting, so the header
     describes what you are looking at rather than the team behind it. */
  const runs = [
    { run_id: "1", provider: "claude", name: "tester", agent: "tester", status: "running" },
    { run_id: "2", provider: "claude", name: "tester", agent: "tester", status: "done" },
    { run_id: "3", provider: "claude", name: "researcher", agent: "researcher", status: "running" },
  ];
  const roleOf = (r: { agent?: string | null; provider?: string }) => r.agent || r.provider || "agent";
  const all = buildRail(runs as never[], "s", () => 0);
  assert.equal(all.runs.length, 3);
  assert.equal(all.filter, undefined, "the whole team is not a place you have to leave");

  const inside = buildRail(runs as never[], "s", () => 0, undefined,
    { agent: "tester", roleOf: roleOf as never });
  // Membership, not order: the rail sorts by who needs you, which is its own tested behaviour.
  assert.deepEqual(inside.runs.map((r) => r.run.run_id).sort(), ["1", "2"]);
  assert.equal(inside.filter, "tester", "the breadcrumb needs to know where you are");
});

test("the panel can take you into an agent and back out again", () => {
  const seen: (string | null)[] = [];
  const handlers = {
    run: () => {}, open: () => {}, act: () => {},
    agent: (id: string | null) => seen.push(id),
  };
  railRoute({ type: "agent", id: "visual-critic" }, handlers);
  railRoute({ type: "agent", id: "" }, handlers);
  assert.deepEqual(seen, ["visual-critic", null], "an empty id means: back to the whole team");
});

test("the team's list holds the team; your own windows are a band of their own", () => {
  /* "From the side panel, i still don't have only the conversations or things i interact with" —
     so the TEAM list stays free of them. But the company you watch includes the sessions this
     machine is actually running, newest first, each one a real place to go. */
  const runs = [
    { run_id: "1", provider: "claude", name: "tester", agent: "tester", status: "running" },
    { run_id: "2", provider: "claude", name: "claude", status: "foreign", started_at: 10 },
    { run_id: "3", provider: "claude", name: "claude", status: "foreign", started_at: 20 },
  ];
  const rail = buildRail(runs as never[], "s", () => 0);
  assert.deepEqual(rail.runs.map((r) => r.run.run_id), ["1"]);
  assert.ok(!rail.runs.some((r) => r.attention === "not-ours"), "no greyed rows in the team list");
  assert.deepEqual(rail.yours?.map((r) => r.run.run_id), ["3", "2"], "newest of yours first");
});

test("inside a drill-in your own sessions do not follow", () => {
  const built = buildRail(
    [run({ run_id: "t1", name: "tester", status: "running" }),
     run({ run_id: "mine", status: "foreign" })] as never[],
    "s", (() => 0) as never, (() => false) as never,
    { agent: "tester", roleOf: (r: { name: string }) => r.name } as never);
  assert.equal(built.yours?.length, 0, "an agent's page is that agent's history, nothing else");
});

test("the destinations are the panel's own, not a launcher for everything", () => {
  /* "The sidepanel is there to view info about who we click on, and view the conversation... That's
     all." Surfaces that live elsewhere (the dashboard, the sequence view) stay reachable from the
     palette; they do not take space in a 299px column whose job is the roster and the reply. */
  assert.ok(CHIPS.length <= 3, `${CHIPS.length} destinations is a launcher, not a panel`);
  for (const c of CHIPS) {
    assert.ok(c.label && c.command, "a destination needs a word and something to do");
  }
});

test("the roster's rows are the map's sprites — one entity model per screen", () => {
  /* The cold sweep's root coherence finding: the map draws one sprite per AGENT (~6) while the
     roster beside it lists 16 RUNS, and nothing reconciles them — two researcher runs, one
     character. At the top level a row IS an agent: worst errand speaks, the count says how many.
     Drilling into an agent still lists its runs — that is what the depth is FOR. */
  const runs = [
    { run_id: "r1", provider: "claude", name: "researcher", agent: "researcher", status: "running", started_at: 20 },
    { run_id: "r2", provider: "claude", name: "researcher", agent: "researcher", status: "failed", started_at: 10 },
    { run_id: "t1", provider: "claude", name: "tester", agent: "tester", status: "completed", started_at: 5 },
  ];
  const identify = (r: { agent?: string | null }) => ({ id: r.agent ?? "main", label: r.agent ?? "main" });
  const grouped = buildRail(runs as never[], "s", () => 0, undefined, undefined, identify as never);
  assert.equal(grouped.runs.length, 2, "three errands, two colleagues");
  const researcher = grouped.runs.find((r) => identify(r.run as never).id === "researcher")!;
  assert.equal(researcher.attention, "error", "the errand that needs him speaks for the agent");
  assert.equal(researcher.tasks, 2, "and the count says how much it holds");

  const drilled = buildRail(runs as never[], "s", () => 0, undefined,
    { agent: "researcher", roleOf: (r) => identify(r as never).id }, identify as never);
  assert.equal(drilled.runs.length, 2, "inside an agent, the rows are its errands again");
});

test("the ready company rests in the staff band, present but never competing", () => {
  /* Rows = sprites still holds with the full company drawn: a declared agent is a quiet row in
     the staff band — present, named, zero-count — while the working list stays the working list. */
  const runs = [
    { run_id: "f1", provider: "claude", name: "x", agent: "x", status: "failed", started_at: 9 },
    { run_id: "decl:y", provider: "claude", name: "y", agent: "y", status: "declared" },
  ];
  const identify = (r: { agent?: string | null }) => ({ id: r.agent ?? "m", label: r.agent ?? "m" });
  const rail = buildRail(runs as never[], "s", () => 0, undefined, undefined, identify as never);
  assert.deepEqual(rail.runs.map((r) => r.run.run_id), ["f1"], "an error is the working list's business");
  assert.deepEqual(rail.staff?.map((r) => [r.run.run_id, r.attention, r.tasks]),
    [["decl:y", "ready", 0]]);
});

test("a stopped run outranks the finished and never wears their mark", () => {
  const stopped = attentionOf({ run_id: "a", status: "stopped" } as never, 0);
  assert.equal(stopped, "stopped");
  const done = attentionOf({ run_id: "b", status: "completed" } as never, 0);
  assert.equal(done, "finished");
  assert.ok(RANK.indexOf("stopped") < RANK.indexOf("finished"),
    "worst-of-members must pick STOPPED over ✓ when a group holds both");
});

test("a grouped row speaks the worst of its members", () => {
  /* tester×3 with two done and one stopped must not say ✓. */
  const runs = [
    { run_id: "1", provider: "claude", agent: "t", name: "t", status: "completed", started_at: 3 },
    { run_id: "2", provider: "claude", agent: "t", name: "t", status: "stopped", started_at: 2 },
    { run_id: "3", provider: "claude", agent: "t", name: "t", status: "completed", started_at: 1 },
  ];
  const rail = buildRail(runs as never[], "s", () => 0, undefined, undefined,
    ((r: { agent?: string }) => ({ id: r.agent ?? "x", label: r.agent ?? "x" })) as never);
  assert.equal(rail.runs.length, 1);
  assert.equal(rail.runs[0].attention, "stopped");
});

/* ——— The reception desk: lifecycle bands (the "I still don't like this small menu" round) ——— */

const DAY = 86400;

test("finished work older than a day leaves the list for the ledger", () => {
  const now = 10 * DAY;
  const built = buildRail([
    run({ run_id: "old1", status: "done", started_at: now - 8 * DAY, cost_usd: 1.98 }),
    run({ run_id: "old2", status: "stopped", started_at: now - 7 * DAY, cost_usd: 0.76 }),
    run({ run_id: "live", status: "running", started_at: now - 60 }),
  ] as never[], "interact", (() => 0) as never, (() => false) as never, undefined, undefined, now);
  assert.deepEqual(built.runs.map((r) => r.run.run_id), ["live"], "old work must stop competing");
  assert.equal(built.ledger?.runs.length, 2);
  assert.ok(Math.abs((built.ledger?.cost ?? 0) - 2.74) < 1e-9, "the ledger carries the bill");
  assert.equal(built.header.finished, 0, "the header no longer double-counts the ledger");
});

test("a finish from this morning keeps its seat", () => {
  const now = 10 * DAY;
  const built = buildRail([
    run({ run_id: "fresh", status: "done", started_at: now - 3600 }),
  ] as never[], "interact", (() => 0) as never, (() => false) as never, undefined, undefined, now);
  assert.equal(built.runs.length, 1);
  assert.equal(built.runs[0].attention, "finished");
  assert.equal(built.ledger, null);
});

test("an ancient failure is history, not a standing alarm", () => {
  const now = 10 * DAY;
  const built = buildRail([
    run({ run_id: "oldbad", status: "crashed", started_at: now - 8 * DAY }),
  ] as never[], "interact", (() => 0) as never, (() => false) as never, undefined, undefined, now);
  assert.equal(built.header.needsYou, 0);
  assert.equal(built.ledger?.failed, 1, "but the ledger line says it plainly");
});

test("an agent whose every errand is ancient rests as staff, never as a stale task row", () => {
  const now = 10 * DAY;
  const identify = (r: { name: string }) => ({ id: r.name, label: r.name });
  const built = buildRail([
    run({ run_id: "a1", name: "tester", status: "done", started_at: now - 8 * DAY }),
    run({ run_id: "a2", name: "tester", status: "stopped", started_at: now - 7 * DAY }),
  ] as never[], "interact", (() => 0) as never, (() => false) as never, undefined, identify as never, now);
  assert.equal(built.runs.length, 0, "no 8-day-old junk task speaking for a living agent");
  assert.deepEqual(built.staff.map((r) => r.attention), ["ready"]);
  assert.equal(built.ledger?.runs.length, 2, "the errands themselves are still reachable");
});

test("declared colleagues rest in the staff band, out of the working list", () => {
  const built = rail([
    run({ run_id: "decl:critic", name: "critic", status: "declared" }),
    run({ run_id: "live", status: "running" }),
  ]);
  assert.deepEqual(built.runs.map((r) => r.run.run_id), ["live"]);
  assert.deepEqual(built.staff.map((r) => r.run.run_id), ["decl:critic"]);
});

test("drilling into an agent shows its whole history, ledger and all", () => {
  const now = 10 * DAY;
  const built = buildRail([
    run({ run_id: "old", name: "tester", status: "done", started_at: now - 8 * DAY }),
    run({ run_id: "new", name: "tester", status: "running", started_at: now - 60 }),
  ] as never[], "interact", (() => 0) as never, (() => false) as never,
    { agent: "tester", roleOf: (r: { name: string }) => r.name } as never, undefined, now);
  assert.equal(built.runs.length, 2, "inside an agent you asked for the history");
  assert.equal(built.ledger, null);
});

test("an old failure never outshouts today's work in a grouped row", () => {
  const now = 10 * DAY;
  const identify = (r: { name: string }) => ({ id: r.name, label: r.name });
  const built = buildRail([
    run({ run_id: "oldbad", name: "tester", status: "crashed", started_at: now - 8 * DAY }),
    run({ run_id: "live", name: "tester", status: "running", started_at: now - 60 }),
  ] as never[], "interact", (() => 0) as never, (() => false) as never, undefined, identify as never, now);
  assert.equal(built.runs.length, 1);
  assert.equal(built.runs[0].attention, "working", "the recent run speaks; the relic is ledger");
  assert.equal(built.runs[0].tasks, 1, "the count describes what the row stands for now");
});

test("the rail's first action starts a SESSION, not a staffing decision", () => {
  /* "I should always be able to create a new session." Picking one of forty specialists is a
     staffing decision; a session begins with the entry agent, which is the one that puts the
     others to work. The roster picker stays, one step over. */
  const ids = CHIPS.map((c) => c.id);
  assert.ok(ids.includes("session"), "no way to start a session at all");
  assert.equal(CHIPS.find((c) => c.id === "session")!.command, "interact.agents.newSession");
  assert.ok(CHIPS.some((c) => c.command === "interact.agents.spawn"),
    "the deliberate one-specialist path must stay reachable");
});


test("the visible Settings control opens the dashboard while unknown commands stay blocked", () => {
  assert.deepEqual(railAction({ type: "command", command: "interact.openDashboard" }),
    { kind: "command", command: "interact.openDashboard" });
  assert.equal(railAction({ type: "command", command: "workbench.action.reloadWindow" }), null);
});
