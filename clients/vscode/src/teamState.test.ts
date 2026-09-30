import { strict as assert } from "node:assert";
import { test } from "node:test";

import { activityOf, latestMeaningful, zoneOf, zoneOfSteps } from "./teamState.ts";

// A worker stands where the WORK is. The zone has to come from what the agent is actually doing
// right now, or the workplace is just a list with sprites on it.

test("reading or writing source puts you in the code room", () => {
  for (const tool of ["Read", "Edit", "Write", "Grep", "Glob"]) {
    assert.equal(zoneOf({ kind: "tool", tool }, "running"), "code");
  }
});

test("reaching the web takes you out of the building", () => {
  for (const tool of ["WebFetch", "WebSearch"]) {
    assert.equal(zoneOf({ kind: "tool", tool }, "running"), "web");
  }
});

test("spawning or messaging a teammate is management", () => {
  for (const tool of ["Agent", "Task", "SendMessage", "ListAgents"]) {
    assert.equal(zoneOf({ kind: "tool", tool }, "running"), "managers");
  }
  assert.equal(zoneOf({ kind: "spawn" }, "running"), "managers");
});

test("driving or looking at a screen is studio work", () => {
  for (const tool of ["mcp__galaius__screenshot", "mcp__galaius__run_actions", "review_ui"]) {
    assert.equal(zoneOf({ kind: "tool", tool }, "running"), "studio");
  }
});

test("a shell command is the lab — that is where things get run and measured", () => {
  assert.equal(zoneOf({ kind: "tool", tool: "Bash" }, "running"), "lab");
});

test("thinking with no tool in hand is the managers' room, not limbo", () => {
  assert.equal(zoneOf({ kind: "thinking" }, "running"), "managers");
});

test("a finished worker has gone back to the entrance", () => {
  assert.equal(zoneOf({ kind: "tool", tool: "Read" }, "done"), "entry");
});

test("your own editor sessions stand at the main entrance — that is where work comes from", () => {
  // They are real people working, not idle. But they are never shown mid-task: galaius does not
  // supervise them, and drawing one "in the code" would claim a supervision it does not have.
  assert.equal(zoneOf({ kind: "tool", tool: "Read" }, "foreign"), "entry");
  assert.equal(zoneOf({ kind: "tool", tool: "WebFetch" }, "foreign", "researcher"), "entry");
});

test("an unknown tool does not throw a worker out of the building", () => {
  assert.equal(zoneOf({ kind: "tool", tool: "SomeFutureTool" }, "running"), "managers");
});

// The activity line is what a person would SAY they are doing, not the tool's name.

test("activity names the file, not the verb", () => {
  assert.match(activityOf({ kind: "tool", tool: "Read", tool_input: "file_path='/a/b/reg.py'" }),
    /reg\.py/);
});

test("a tool with no readable argument still says something useful", () => {
  assert.equal(activityOf({ kind: "tool", tool: "Bash", tool_input: "" }), "running Bash");
});

test("plain speech is quoted back, clipped", () => {
  assert.match(activityOf({ kind: "text", text: "I have finished the review of the diff" }),
    /finished the review/);
});

test("nothing recorded reads as waiting, not as blank", () => {
  assert.match(activityOf(undefined), /waiting|idle/i);
});

// Assembling the whole room from what the registry holds.

import { buildTeam } from "./teamState.ts";

const RUNS = [
  { run_id: "lead", name: "reviewer", agent: "code-reviewer", status: "running",
    project: "galaius", cost_usd: 1.2, input_tokens: 90000, parent_run_id: null,
    started_at: 1000, last: "" },
  { run_id: "sub", name: "researcher", agent: "researcher", status: "running",
    project: "galaius", cost_usd: 0.1, input_tokens: 5000, parent_run_id: "lead",
    started_at: 1000, last: "" },
  { run_id: "old", name: "perf", agent: null, status: "done", project: "galaius",
    cost_usd: 0.5, input_tokens: 100, parent_run_id: null, started_at: 1, last: "" },
] as any[];

const STEPS: Record<string, any> = {
  lead: { kind: "tool", tool: "Read", tool_input: "file_path='/a/registry.py'" },
  sub: { kind: "tool", tool: "WebSearch", tool_input: "query='vscode webview'" },
  old: { kind: "text", text: "done" },
};

test("each worker is placed by what they are doing", () => {
  const team = buildTeam(RUNS, (id) => (STEPS[id] ? [STEPS[id]] : []), 2000);
  const byId = Object.fromEntries(team.workers.map((w) => [w.run_id, w]));
  assert.equal(byId.lead.zone, "code");
  assert.equal(byId.sub.zone, "web", "a researcher fetching is out of the building");
  assert.equal(byId.old.zone, "entry", "finished workers come back to the door");
});

test("a sub-agent keeps its parent, so it can be drawn beside them", () => {
  const team = buildTeam(RUNS, (id) => (STEPS[id] ? [STEPS[id]] : []), 2000);
  assert.equal(team.workers.find((w) => w.run_id === "sub")!.parent_run_id, "lead");
});

test("the activity line survives into the state", () => {
  const team = buildTeam(RUNS, (id) => (STEPS[id] ? [STEPS[id]] : []), 2000);
  assert.match(team.workers.find((w) => w.run_id === "lead")!.activity, /registry\.py/);
});

test("idle time is measured, so the view can fade whoever stopped", () => {
  const team = buildTeam(RUNS, (id) => (STEPS[id] ? [STEPS[id]] : []), 5000);
  assert.ok(team.workers.every((w) => w.idle_seconds >= 0));
});

test("an empty registry is an empty room, not a crash", () => {
  assert.deepEqual(buildTeam([], () => [], 1).workers, []);
});

// "researchers accessing the web... the librarian... etc" — a worker has a HOME room from who
// they are, not only from the tool they last touched. A thinking librarian belongs in the
// library, not parked in a generic managers box.

test("an agent's role gives it a home room when the work does not name one", () => {
  assert.equal(zoneOf({ kind: "thinking" }, "running", "librarian"), "library");
  assert.equal(zoneOf({ kind: "thinking" }, "running", "researcher"), "web");
  assert.equal(zoneOf({ kind: "thinking" }, "running", "visual-critic"), "studio");
  assert.equal(zoneOf({ kind: "thinking" }, "running", "artist"), "studio");
  assert.equal(zoneOf({ kind: "thinking" }, "running", "tester"), "lab");
  assert.equal(zoneOf({ kind: "thinking" }, "running", "perf-critic"), "lab");
  assert.equal(zoneOf({ kind: "thinking" }, "running", "optimizer"), "lab");
  assert.equal(zoneOf({ kind: "thinking" }, "running", "code-reviewer"), "code");
});

test("the work in hand still wins over the role — that is the point of watching", () => {
  // A librarian reading source is IN the code, not at their desk. Position must be earned.
  assert.equal(zoneOf({ kind: "tool", tool: "Read" }, "running", "librarian"), "code");
  assert.equal(zoneOf({ kind: "tool", tool: "WebFetch" }, "running", "tester"), "web");
});

test("an unknown role falls back to the managers' room, not to nowhere", () => {
  assert.equal(zoneOf({ kind: "thinking" }, "running", "some-new-agent"), "managers");
  assert.equal(zoneOf({ kind: "thinking" }, "running", null), "managers");
});

test("a finished worker is at the entrance whatever their role", () => {
  assert.equal(zoneOf({ kind: "thinking" }, "done", "librarian"), "entry");
});

test("your own session says what it is, not that it is waiting", () => {
  // galaius does not supervise your editor sessions, so it must not narrate their work — but
  // "waiting" is worse than saying nothing: it claims they are doing nothing at all.
  const team = buildTeam(
    [{ run_id: "mine", name: "galaius-32", status: "foreign", parent_run_id: null,
       started_at: 1, project: "galaius" } as any],
    () => [],
    100,
  );
  assert.equal(team.workers[0].zone, "entry");
  assert.match(team.workers[0].activity, /your (own )?session|not started by galaius/i);
});

// "And be able to see the agents actually communicate (send messages etc...)" — a room full of
// people who never speak to each other is a set of processes, not a team.

test("an exchange between two present workers becomes a link", () => {
  const team = buildTeam(RUNS, (id) => (STEPS[id] ? [STEPS[id]] : []), 2000,
    [{ from_run: "lead", to_run: "sub", text: "check the docs too" }]);
  assert.deepEqual(team.links, [{ from_run_id: "lead", to_run_id: "sub", text: "check the docs too", at: null }]);
});

test("a link to someone who has been forgotten is dropped, not drawn at nobody", () => {
  const team = buildTeam(RUNS, (id) => (STEPS[id] ? [STEPS[id]] : []), 2000,
    [{ from_run: "lead", to_run: "vanished", text: "hello" }]);
  assert.deepEqual(team.links, []);
});

test("a message from the operator is not an agent-to-agent link", () => {
  const team = buildTeam(RUNS, (id) => (STEPS[id] ? [STEPS[id]] : []), 2000,
    [{ from_run: "operator", to_run: "lead", text: "do it" }]);
  assert.deepEqual(team.links, []);
});

// The newest recorded event is often housekeeping, so "the last step" is not "what they are
// doing". Taking it literally showed a researcher mid-web-search as "waiting".

test("the newest MEANINGFUL step is what a worker is doing", () => {
  const steps = [
    { kind: "tool", tool: "WebSearch", tool_input: "query='vscode webview'" },
    { kind: "other", text: "" },
    { kind: "other", text: "" },
  ];
  assert.equal(latestMeaningful(steps)?.tool, "WebSearch");
});

test("a later real step wins over an earlier one", () => {
  const steps = [
    { kind: "tool", tool: "Read" },
    { kind: "tool", tool: "WebSearch" },
    { kind: "other" },
  ];
  assert.equal(latestMeaningful(steps)?.tool, "WebSearch");
});

test("nothing meaningful at all is undefined, which reads as waiting", () => {
  assert.equal(latestMeaningful([{ kind: "other" }, { kind: "rate_limit" }]), undefined);
  assert.equal(latestMeaningful([]), undefined);
});

test("a tool RESULT is not what someone is doing — the tool call is", () => {
  // Live capture: a reviewer reading a file showed `84: ? \`<span class="wp-...` as its activity,
  // which is the file's contents. A person says "reading style.ts", not the bytes they got back.
  const steps = [
    { kind: "tool", tool: "Read", tool_input: "file_path='/a/style.ts'" },
    { kind: "tool_result", text: "84: ? `<span class=\"wp-snooze\">${draw(SNOOZE.grid)}" },
  ];
  assert.equal(latestMeaningful(steps)?.tool, "Read");
  assert.match(activityOf(latestMeaningful(steps)), /reading style\.ts/);
});

// The activity line is read by a person at a glance, so its subject has to be a NAME — a file, a
// query, a target. Live capture showed `running \${\` after a shell command whose arguments
// contained template literals: the quoted-string fallback grabbed code, not a name.

test("a shell command names the command, never a fragment of its code", () => {
  const step = { kind: "tool", tool: "Bash", tool_input: "command='echo \"${x}\" | grep -o \\`y\\`'" };
  const said = activityOf(step);
  assert.ok(!/[${}`\\]/.test(said), `activity should carry no code noise: ${said}`);
});

test("a path is still preferred as the subject", () => {
  assert.match(activityOf({ kind: "tool", tool: "Read", tool_input: "file_path='/a/b/style.ts'" }),
    /reading style\.ts/);
});

test("a plain quoted phrase is still a good subject", () => {
  assert.match(activityOf({ kind: "tool", tool: "WebSearch", tool_input: "query='pixel art css'" }),
    /pixel art css/);
});

// From a code-reviewer run on this diff: a denylist leaks. A vendor-added kind we have never seen
// would become someone's activity line, and silencing `done` made a finished run narrate its last
// utterance forever instead of saying it had finished.

test("a kind nobody has taught us is silent, not leaked onto the plate", () => {
  assert.equal(latestMeaningful([{ kind: "text", text: "hi" }, { kind: "some_future_kind" }])?.kind,
    "text");
});

test("finishing is meaningful — it is the last true thing about a run", () => {
  assert.equal(latestMeaningful([{ kind: "text", text: "hi" }, { kind: "done" }])?.kind, "done");
});

test("a tool call still beats the result that follows it", () => {
  assert.equal(
    latestMeaningful([{ kind: "tool", tool: "WebSearch" }, { kind: "tool_result", text: "..." }])?.tool,
    "WebSearch",
  );
});

// visual-critic, live: every finished agent lost its room and collapsed into ENTRY as a flat
// grid — "exactly the undifferentiated-grid look the redesign was meant to replace", and finished
// is the DOMINANT real state. A worker stays where it last worked; only someone who never worked
// anywhere stands at the door.

test("a finished worker keeps the room it was last working in", () => {
  const steps = [
    { kind: "tool", tool: "WebSearch", tool_input: "query='x'" },
    { kind: "text", text: "found it" },
    { kind: "done" },
  ];
  assert.equal(zoneOfSteps(steps, "done", "researcher"), "web");
});

test("a finished worker that only ever thought falls back to its role's room", () => {
  assert.equal(zoneOfSteps([{ kind: "thinking" }, { kind: "done" }], "done", "librarian"), "library");
});

test("a worker that never did anything at all stands at the entrance", () => {
  assert.equal(zoneOfSteps([], "done", null), "entry");
});

test("a running worker is still placed by what it is doing right now", () => {
  const steps = [{ kind: "tool", tool: "Read" }, { kind: "tool", tool: "WebFetch" }];
  assert.equal(zoneOfSteps(steps, "running", "code-reviewer"), "web");
});

test("your own session still stands at the entrance", () => {
  assert.equal(zoneOfSteps([{ kind: "tool", tool: "Read" }], "foreign", null), "entry");
});


// --- idle_seconds was measuring the wrong thing entirely ---
//
// It was `now - (finished_at ?? started_at)`. For a RUNNING agent finished_at is null, so it
// returned TOTAL ELAPSED SINCE START — and the view stamps HELD at 120s and cuts the ambient
// animation. So every agent that had been working for more than two minutes was drawn asleep,
// and the harder it worked the deader the building looked. Idleness is time since the last thing
// the agent was OBSERVED doing, which is a different clock.

test("an agent working steadily is not idle, however long it has been at it", () => {
  const runs = [{ run_id: "busy", name: "busy", status: "running", started_at: 1000 }] as never;
  const steps = () => [{ kind: "tool", tool: "Read", tool_input: "a.py", at: 4990 }] as never;

  const team = buildTeam(runs, steps, 5000);

  assert.ok(
    team.workers[0].idle_seconds < 30,
    `busy for an hour, last seen 10s ago, reported idle for ${team.workers[0].idle_seconds}s`,
  );
});

test("an agent that has genuinely gone quiet IS idle", () => {
  const runs = [{ run_id: "quiet", name: "quiet", status: "running", started_at: 1000 }] as never;
  const steps = () => [{ kind: "tool", tool: "Read", tool_input: "a.py", at: 2000 }] as never;

  const team = buildTeam(runs, steps, 5000);

  assert.equal(team.workers[0].idle_seconds, 3000);
});

test("with no observation time at all, a running agent is not accused of being idle", () => {
  // Records written before observation stamping existed. A false "held" is worse than no held:
  // it puts a sleeping stamp on someone who is working.
  const runs = [{ run_id: "old", name: "old", status: "running", started_at: 1000 }] as never;
  const steps = () => [{ kind: "tool", tool: "Read", tool_input: "a.py" }] as never;

  assert.equal(buildTeam(runs, steps, 9999).workers[0].idle_seconds, 0);
});

test("a finished agent's clock still stops when it finished", () => {
  const runs = [
    { run_id: "done", name: "done", status: "done", started_at: 1000, finished_at: 4000 },
  ] as never;

  assert.equal(buildTeam(runs, () => [] as never, 5000).workers[0].idle_seconds, 1000);
});

test("a worker carries its own clock, so no view has to invent one", () => {
  const runs = [
    { run_id: "r", name: "r", status: "done", started_at: 1000, finished_at: 4000 },
  ] as never;
  const w = buildTeam(runs, () => [] as never, 5000).workers[0];

  assert.equal(w.started_at, 1000);
  assert.equal(w.finished_at, 4000);
});

test("an exchange carries when it happened, so it can be shown as it happens", () => {
  const runs = [
    { run_id: "a", name: "a", status: "running" },
    { run_id: "b", name: "b", status: "running" },
  ] as never;
  const team = buildTeam(runs, () => [] as never, 5000, [
    { from_run: "a", to_run: "b", text: "take a look", at: 4990 },
  ] as never);

  assert.equal(team.links[0].at, 4990);
});

test("the snapshot clock is in SECONDS, the unit Python writes", () => {
  // A bare `number` here cost a 1970 clock in the panel: the view read it as milliseconds, and
  // every dev fixture happened to stamp milliseconds, so the harness agreed with the bug. Pinned
  // so the contract is checked rather than remembered.
  const team = buildTeam([] as never, () => [] as never, 1_700_000_000);
  assert.equal(team.at, 1_700_000_000);
  assert.ok(team.at < 2_000_000_000, "a millisecond value would be ~1.7e12 and read as year 55000");
});

test("the default clock is seconds too, not Date.now()", () => {
  const team = buildTeam([] as never, () => [] as never);
  assert.ok(team.at < 2_000_000_000, `default clock looks like milliseconds: ${team.at}`);
});

// The floor and the tree must agree about a run that went wrong. `status` was cast straight from
// the registry with `as Worker["status"]`, which silenced TypeScript over a genuine mismatch: the
// registry says failed / interrupted / stopped, the floor's stamp table only knows running / done /
// error / foreign. So a CRASHED agent got no stamp at all — the tree drew it red while the floor
// showed it as if nothing had happened.
test("every status the registry can produce reaches the floor as one it understands", () => {
  const REGISTRY_STATES = ["running", "done", "failed", "interrupted", "stopped", "foreign"];
  const FLOOR_STATES = new Set(["running", "done", "error", "foreign"]);
  for (const status of REGISTRY_STATES) {
    const [worker] = buildTeam(
      [{ run_id: "r", name: "w", status } as never], () => [], 2000,
    ).workers;
    assert.ok(FLOOR_STATES.has(worker.status),
      `${status} reaches the floor as ${worker.status}, which its stamp table cannot render`);
  }
});

test("a run that failed or was interrupted is shown as an error, not as nothing", () => {
  for (const status of ["failed", "interrupted"]) {
    const [worker] = buildTeam(
      [{ run_id: "r", name: "w", status } as never], () => [], 2000,
    ).workers;
    assert.equal(worker.status, "error", `${status} does not read as an error on the floor`);
  }
});

// "Agent inherit all the capabilities they have through files, and they should have actions for
// these." The definitions declare `tools:` in their frontmatter and nothing read it, so the world
// drew a character that can only read files exactly like one that can drive a browser and spawn
// others. buildTeam now carries what each one can DO, read from its own definition.
test("a worker carries the faculties its definition declares", () => {
  const [worker] = buildTeam(
    [{ run_id: "r", name: "artist", agent: "artist", status: "running",
       definition_path: "/defs/artist.md" } as never],
    () => [], 2000, [],
    // The caller resolves this from the definition file; here it stands in for that read.
    (run) => (run.definition_path === "/defs/artist.md"
      ? ["reads", "writes", "runs", "sees"] : []),
  ).workers;
  assert.deepEqual(worker.faculties, ["reads", "writes", "runs", "sees"]);
});

test("a run with no definition claims no faculties rather than guessing", () => {
  // A plain `claude` run has no definition file. Inventing powers for it would misreport what is
  // loose in your workspace, which is the one thing this is for.
  const [worker] = buildTeam(
    [{ run_id: "r", name: "main", agent: null, status: "running" } as never],
    () => [], 2000, [], () => [],
  ).workers;
  assert.deepEqual(worker.faculties, []);
});

test("an unreadable definition degrades to no faculties, never to a crash", () => {
  const [worker] = buildTeam(
    [{ run_id: "r", name: "x", agent: "x", status: "running",
       definition_path: "/gone.md" } as never],
    () => [], 2000, [],
    () => { throw new Error("ENOENT"); },
  ).workers;
  assert.deepEqual(worker.faculties, []);
});

// "You could have a main brain kind of agent for the first agent we ask to do something."
// The team has an orchestrator — the run YOU started, which then put everyone else to work — and
// nothing in the world said so. Every character stood at the same rank, which is why the building
// read as a bag of sprites rather than a company.
test("the first agent you asked, that others report to, is the brain", () => {
  const team = buildTeam([
    { run_id: "boss", name: "main", status: "running", started_at: 100 },
    { run_id: "kid", name: "tester", status: "running", parent_run_id: "boss", started_at: 200 },
    { run_id: "kid2", name: "artist", status: "running", parent_run_id: "boss", started_at: 300 },
  ] as never[], () => [], 2000);
  const by = Object.fromEntries(team.workers.map((w) => [w.run_id, w]));
  assert.equal(by.boss.brain, true);
  assert.equal(by.kid.brain, false);
  assert.equal(by.kid2.brain, false);
});

test("with several roots, the one that started first is the brain", () => {
  // Two unrelated sessions both have no parent. The brain is the one whose work began the arc,
  // not whichever the filesystem happened to list first.
  const team = buildTeam([
    { run_id: "later", name: "b", status: "running", started_at: 900 },
    { run_id: "earlier", name: "a", status: "running", started_at: 100 },
  ] as never[], () => [], 2000);
  const by = Object.fromEntries(team.workers.map((w) => [w.run_id, w]));
  assert.equal(by.earlier.brain, true);
  assert.equal(by.later.brain, false);
});

test("one of your own editor sessions is never the brain", () => {
  // galaius does not drive it, so crowning it would claim an authority the view does not have.
  const team = buildTeam([
    { run_id: "mine", name: "my window", status: "foreign", started_at: 1 },
    { run_id: "ours", name: "main", status: "running", started_at: 500 },
  ] as never[], () => [], 2000);
  const by = Object.fromEntries(team.workers.map((w) => [w.run_id, w]));
  assert.equal(by.mine.brain, false);
  assert.equal(by.ours.brain, true);
});

test("exactly one brain, ever", () => {
  const team = buildTeam([
    { run_id: "a", name: "a", status: "running", started_at: 100 },
    { run_id: "b", name: "b", status: "running", started_at: 100 },
    { run_id: "c", name: "c", status: "running", parent_run_id: "a", started_at: 200 },
  ] as never[], () => [], 2000);
  assert.equal(team.workers.filter((w) => w.brain).length, 1);
});

test("an empty team has no brain rather than a phantom one", () => {
  assert.deepEqual(buildTeam([], () => [], 2000).workers, []);
});

// "You can split in room kind of things for each domain of work (for instance finance would be
// elsewhere)." The org file already declares departments with rooms — Quality & Critics,
// Production & Makers, Research & Intelligence, Office of Records, Finance Desk — and placement
// ignored every one of them, so a finance agent stood in the same room as a code reviewer.
test("a worker carries the domain its definition belongs to", () => {
  const [worker] = buildTeam(
    [{ run_id: "r", name: "auditor", agent: "auditor", status: "running" } as never],
    () => [], 2000, [], () => [],
    (agent) => (agent === "auditor"
      ? { id: "finance", room: "Finance Desk" } : null),
  ).workers;
  assert.equal(worker.department, "finance");
  assert.equal(worker.room, "Finance Desk");
});

test("an agent in no department claims none rather than being filed somewhere wrong", () => {
  // A wrong room silently merges unrelated work, which is worse than an unplaced character.
  const [worker] = buildTeam(
    [{ run_id: "r", name: "main", agent: null, status: "running" } as never],
    () => [], 2000, [], () => [], () => null,
  ).workers;
  assert.equal(worker.department, undefined);
  assert.equal(worker.room, undefined);
});

test("a resolver that throws leaves the character unplaced, never crashes the building", () => {
  const [worker] = buildTeam(
    [{ run_id: "r", name: "x", agent: "x", status: "running" } as never],
    () => [], 2000, [], () => [],
    () => { throw new Error("no org file"); },
  ).workers;
  assert.equal(worker.department, undefined);
});

test("one character per agent, not one per errand", () => {
  /* His words: "Galaius Team still does not have the correct or all the agents. I can see multiple
     'claude' agents... They're all duplicates or don't have the correct name."

     The world drew one body per RUN, so an agent asked to do three things stood in the room three
     times, and the five bare sessions in his registry all rendered as identical "claude" figures.
     An agent is a PERSON; the errands are what it was given. The roster already made that
     distinction; the world had not. */
  const runs = [
    { run_id: "r1", name: "researcher", agent: "researcher", status: "done", started_at: 10 },
    { run_id: "r2", name: "researcher", agent: "researcher", status: "running", started_at: 20 },
    { run_id: "r3", name: "researcher", agent: "researcher", status: "done", started_at: 30 },
    { run_id: "t1", name: "tester", agent: "tester", status: "done", started_at: 5 },
  ];
  const team = buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    (r) => ({ id: (r as { agent?: string | null }).agent ?? "main", label: (r as { agent?: string | null }).agent ?? "main" }));
  assert.deepEqual(team.workers.map((w) => w.name).sort(), ["researcher", "tester"]);
  const who = team.workers.find((w) => w.name === "researcher")!;
  assert.equal(who.tasks, 3, "the character should carry how many errands it was given");
});

test("bare sessions collapse into the one colleague they actually are", () => {
  /* Five runs recorded as `claude` with no definition are not five teammates — they are the
     coordinator, five times. */
  const runs = [1, 2, 3, 4, 5].map((n) => ({
    run_id: `c${n}`, name: "claude", agent: null, status: "done", started_at: n,
  }));
  const team = buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    () => ({ id: "main", label: "Main thread" }));
  assert.equal(team.workers.length, 1, "five identical figures is what he is looking at");
  assert.equal(team.workers[0].name, "Main thread");
  assert.equal(team.workers[0].tasks, 5);
});

test("the character shows the state that most needs you", () => {
  /* An agent with a failed errand and two finished ones is not "done" — the eye must land on the
     one that needs him. */
  const runs = [
    { run_id: "a", name: "x", agent: "x", status: "done", started_at: 1 },
    { run_id: "b", name: "x", agent: "x", status: "failed", started_at: 2 },
    { run_id: "c", name: "x", agent: "x", status: "done", started_at: 3 },
  ];
  const team = buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    () => ({ id: "x", label: "x" }));
  assert.equal(team.workers.length, 1);
  assert.equal(team.workers[0].status, "error");
});

test("a character opens the agent, so its run_id is one of its own errands", () => {
  const runs = [
    { run_id: "a", name: "x", agent: "x", status: "done", started_at: 1 },
    { run_id: "b", name: "x", agent: "x", status: "running", started_at: 2 },
  ];
  const team = buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    () => ({ id: "x", label: "x" }));
  assert.ok(["a", "b"].includes(team.workers[0].run_id));
});

test("one body carries the cost of ALL its errands", () => {
  /* The cold sweep found two team totals on one screen: the map's header said $12.56 while the
     dashboard said $20.81 over the same sixteen agents. The dedup did it — a worker carried only
     its SPEAKING run's cost, silently dropping every other errand's. One agent, one body, the sum
     of everything it was asked to do. */
  const runs = [
    { run_id: "a", name: "x", agent: "x", status: "done", started_at: 1, cost_usd: 2.5, input_tokens: 100 },
    { run_id: "b", name: "x", agent: "x", status: "done", started_at: 2, cost_usd: 1.5, input_tokens: 50 },
  ];
  const team = buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    () => ({ id: "x", label: "x" }));
  assert.equal(team.workers.length, 1);
  assert.equal(team.workers[0].cost_usd, 4, "half the money vanished with the dedup");
  assert.equal(team.workers[0].input_tokens, 150);
});

test("the speaking run never flickers between renders", () => {
  /* Two errands, same status, same start: the old sort left the winner to engine whim, and the
     nameplate flickered between two runs' ages live. A tie breaks the same way every frame. */
  const runs = [
    { run_id: "zz", name: "x", agent: "x", status: "done", started_at: 5 },
    { run_id: "aa", name: "x", agent: "x", status: "done", started_at: 5 },
  ];
  const pick = () => buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    () => ({ id: "x", label: "x" })).workers[0].run_id;
  const first = pick();
  runs.reverse();
  assert.equal(pick(), first, "input order must not choose the face of the character");
});

test("every declared agent stands in the world, asked or not", () => {
  /* "We don't have all the agents! Some other agents exist but aren't used... We should have rooms
     for all areas of agents... Even all the finance agents... In any projects..."

     The world drew only agents WITH runs, so a 13-member Finance Desk rendered as an empty room and
     'production' looked like one lonely teacher. The company file declares the roster; the world
     seats it. An agent nobody has asked yet is READY at its desk, not absent. */
  const runs = [
    { run_id: "r1", name: "researcher", agent: "researcher", status: "running", started_at: 5 },
    { run_id: "decl:auditor", name: "Auditor", agent: "auditor", status: "declared" },
    { run_id: "decl:artist", name: "Artist", agent: "artist", status: "declared" },
  ];
  const team = buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    (r) => ({ id: (r as { agent?: string }).agent ?? "main", label: (r as { name?: string }).name ?? "x" }));
  assert.equal(team.workers.length, 3);
  const ready = team.workers.find((w) => w.agent === "auditor")!;
  assert.equal(ready.tasks, 0, "never asked means zero errands, not a fake one");
  assert.equal(ready.status, "ready",
    "ready reaches the world as itself — standing at ease, not lounging like a finisher");
});

test("a declared agent can never be crowned the brain", () => {
  /* The brain is the first agent YOU asked. With no runs at all there is no brain — crowning a
     random ready agent would invent an orchestrator nobody hired. */
  const runs = [
    { run_id: "decl:a", name: "a", agent: "a", status: "declared" },
    { run_id: "decl:b", name: "b", agent: "b", status: "declared" },
  ];
  const team = buildTeam(runs as never[], () => [], 100, [], () => [], () => null,
    (r) => ({ id: (r as { agent?: string }).agent ?? "x", label: "x" }));
  assert.ok(!team.workers.some((w) => w.brain), "an empty project has no brain");
});
