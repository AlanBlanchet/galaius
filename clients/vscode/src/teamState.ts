/** Turning what an agent is DOING into where it stands.
 *
 *  The workplace only means anything if a worker's position is earned: someone in the Web room is
 *  there because they're fetching a page right now. This is the pure half of that — no I/O, no
 *  rendering — so the mapping can be argued with in tests, not eyeballed on a canvas.
 */
import type { Link, TeamState, Worker, ZoneId } from "./team";

/** One recorded step, as the panel already reads them. */
export interface Step {
  kind: string;
  tool?: string | null;
  tool_input?: string;
  text?: string;
}

/** Tools grouped by the ROOM they are done in, not by what they technically are. A screenshot and
 *  a click are both "studio" because judging a surface is one job done in one place. */
const ROOMS: [ZoneId, RegExp][] = [
  ["web", /^(WebFetch|WebSearch)$/],
  ["code", /^(Read|Edit|Write|Grep|Glob|NotebookEdit|MultiEdit)$/],
  ["managers", /^(Agent|Task|SendMessage|ListAgents|TaskOutput|TaskStop)$/],
  // Anything that looks at or drives a surface — galaius's own tools live here.
  ["studio", /(screenshot|run_actions|review_ui|verify_ui|measure_ui|launch_app|record|get_interactive)/i],
  ["lab", /^(Bash|BashOutput|KillShell)$/],
  ["data", /(registry|cache|catalog|benchmark)/i],
  ["library", /(Skill|librarian|prompt)/i],
];

/** A worker's HOME room — where they sit when the work in hand does not put them somewhere else.
 *  A researcher belongs at the web, a librarian in the library; parking every thinking agent in
 *  one generic box loses exactly the thing that makes a team legible. */
const HOME: [ZoneId, RegExp][] = [
  ["web", /^(researcher|scraper)$/],
  ["library", /^(librarian|teacher|advocate)$/],
  ["studio", /^(visual-critic|artist|ux-critic|audio-critic)$/],
  ["lab", /^(tester|perf-critic|optimizer|session-auditor)$/],
  ["code", /^(code-reviewer|generalizer|threat-modeler)$/],
  ["data", /^(business-strategist)$/],
];

/** Where a worker stands.
 *
 *  Three things decide it, in order. Status first: a finished worker is back at the entrance
 *  whatever it was last holding, and a session galaius didn't start is never shown mid-task —
 *  watching it work would claim a supervision we don't have. Then the WORK in hand, because a
 *  position has to be earned: a librarian reading source is in the code, not at their desk.
 *  Only then their ROLE, so a thinking agent sits somewhere that means something.
 */
export function zoneOf(step: Step | undefined, status: string, agent?: string | null): ZoneId {
  // Own editor sessions stand at the main entrance: where work originates. Never placed mid-task
  // — galaius doesn't supervise them (see doc above).
  if (status === "foreign") return "entry";
  if (status !== "running") return "entry";
  if (step?.kind === "spawn") return "managers";
  if (step?.kind === "tool" && step.tool) {
    for (const [zone, pattern] of ROOMS) {
      if (pattern.test(step.tool)) return zone;
    }
  }
  if (agent) {
    for (const [zone, pattern] of HOME) {
      if (pattern.test(agent)) return zone;
    }
  }
  if (!step) return "idle";
  // Thinking, speaking, or a tool nobody has taught us: still at their desk, not thrown out.
  return "managers";
}

/** Where a worker stands, given everything it has done.
 *
 *  A FINISHED worker keeps the room it was last working in. Sending it back to the entrance
 *  emptied every room the moment a team stopped — the state a team is in most of the time —
 *  collapsing the building into one crowded grid, the exact shape this view replaced. Only
 *  someone who never worked anywhere stands at the door.
 */
export function zoneOfSteps(steps: Step[], status: string, agent?: string | null): ZoneId {
  if (status === "foreign") return "entry";
  if (status === "running") return zoneOf(latestMeaningful(steps), status, agent);
  // Walk back to the last step that actually put them somewhere.
  for (let i = steps.length - 1; i >= 0; i--) {
    const zone = zoneOf(steps[i], "running", null);
    if (zone !== "managers" && zone !== "idle") return zone;
  }
  const home = agent ? zoneOf({ kind: "thinking" }, "running", agent) : "managers";
  return home === "managers" ? "entry" : home;
}

//: Kinds that say nothing about what a worker is DOING. Vendor emits housekeeping constantly, so
//: the newest event is usually one of these — taking it literally showed a researcher mid-search
//: as "waiting".
//: An ALLOWLIST, not a denylist: an untaught kind defaults to silent rather than leaking onto a
//: worker's plate. tool_result is deliberately absent — meaningful, but it's what came BACK, not
//: what the worker is doing; showing it put raw file bytes in a speech bubble where "reading
//: style.ts" belonged.
const MEANINGFUL = new Set(["tool", "spawn", "thinking", "message", "text", "done", "error"]);

/** Mirrors RECENT_SECONDS in rail.ts (the rule's home) — how long finished work stays "current".
 *  Duplicated for the same mechanical reason as HELD_SECONDS: the test loader can't resolve a
 *  shared import, so EQUALITY is enforced by the brain parity test. */
const RECENT_SECONDS = 86400;

/** The most recent step that actually says something, oldest-first input. */
export function latestMeaningful(steps: Step[]): Step | undefined {
  for (let i = steps.length - 1; i >= 0; i--) {
    if (MEANINGFUL.has(steps[i].kind)) return steps[i];
  }
  return undefined;
}

/** What a person would NAME as the subject of the work: a file, a query, a target.
 *
 *  A path wins. Otherwise a quoted phrase, but only one reading as a name — a shell command's
 *  arguments are full of quoted CODE, and taking it produced "running \${\" in a speech bubble
 *  where a filename belonged.
 */
function subject(input: string | undefined): string | null {
  const path = (input ?? "").match(/[\w./-]*\/([\w.-]+\.\w+)/);
  if (path) return path[1];
  for (const match of (input ?? "").matchAll(/'([^']{3,40})'|"([^"]{3,40})"/g)) {
    const value = (match[1] ?? match[2] ?? "").trim();
    // Letters, digits and the punctuation a name actually contains — nothing that reads as code.
    if (value && /^[\w .,'’&:/-]+$/.test(value) && /[a-zA-Z]/.test(value)) return value;
  }
  return null;
}

/** What they would SAY they are doing. "reading registry.py", not "tool: Read". */
export function activityOf(step: Step | undefined): string {
  if (!step) return "waiting";
  if (step.kind === "tool" && step.tool) {
    const what = subject(step.tool_input);
    // With nothing to name, the verb alone reads oddly ("reading"), so fall back to the tool.
    if (!what) return `running ${step.tool}`;
    return `${VERBS[step.tool] ?? "running"} ${what}`;
  }
  if (step.kind === "spawn") return `sending out ${step.text || "a teammate"}`;
  if (step.kind === "thinking") return "thinking";
  if (step.kind === "done") return "finished";
  if (step.kind === "error") return step.text || "failed";
  if (step.kind === "message") return "in conversation";
  const said = (step.text ?? "").trim().replace(/\s+/g, " ");
  return said ? clip(said) : "waiting";
}

/** Which state most needs you, worst first. A character with a failed errand and two finished ones
 *  is not "done" — the eye has to land on the one that needs him. */
const NEEDS_YOU: readonly string[] = ["error", "running", "done", "ready", "foreign"];

const VERBS: Record<string, string> = {
  Read: "reading",
  Edit: "editing",
  Write: "writing",
  Grep: "searching",
  Glob: "looking for",
  Bash: "running",
  WebFetch: "fetching",
  WebSearch: "searching the web for",
  Agent: "briefing",
  Task: "briefing",
  SendMessage: "messaging",
};

function clip(text: string): string {
  return text.length <= 48 ? text : `${text.slice(0, 47).trimEnd()}…`;
}


/** A run as the registry stores it — only the fields the workplace needs. */
export interface RunLike {
  run_id: string;
  name: string;
  agent?: string | null;
  status: string;
  project?: string;
  cost_usd?: number | null;
  input_tokens?: number | null;
  parent_run_id?: string | null;
  finished_at?: number | null;
  started_at?: number;
  /** Where this run's definition lives, so a caller can read what it is allowed to do. Resolved
   *  by the registry at spawn; absent for a plain run with no definition. */
  definition_path?: string | null;
}

/** When this agent was last SEEN doing something, if the events carry a time at all.
 *
 *  galaius observes the vendor's stream, stamping when it first saw a line even though the
 *  vendor writes no timestamp of its own. That observation time is exactly the right clock for a
 *  watched workplace: not when the agent acted (unknowable), but when we noticed.
 */
export function lastObservedAt(steps: Step[]): number | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    const at = (steps[i] as { at?: number }).at;
    if (typeof at === "number" && at > 0) return at;
  }
  return null;
}


/** The domain a run belongs to, or nothing.
 *
 *  Nothing rather than a guess: filing a character into the wrong room silently merges unrelated
 *  work — the same reason project_for refuses to name a project it cannot derive. A resolver
 *  that throws (no company file at all, which is the common case) leaves it unplaced.
 */
function placeByDomain(
  agent: string | null | undefined,
  resolve: (agent: string) => { id: string; room?: string | null } | null,
): { department?: string; room?: string } {
  if (!agent) return {};
  try {
    const found = resolve(agent);
    if (!found) return {};
    return found.room ? { department: found.id, room: found.room } : { department: found.id };
  } catch {
    return {};
  }
}

/** Which run is the orchestrator — the first one YOU asked for something.
 *
 *  A ROOT (nobody sent it) that others report to, earliest first. Roots ranked by start time
 *  rather than array order, so the answer doesn't depend on how the filesystem happened to list
 *  records. Your own editor sessions are excluded: galaius doesn't drive them, so putting one
 *  at the head of the company would claim authority this view doesn't have.
 */
function brainOf(runs: readonly RunLike[], nowSeconds?: number): string | null {
  // Declared-but-never-asked agents aren't candidates: with no runs at all there's no brain, and
  // crowning a ready desk would invent an orchestrator nobody hired.
  // Crown also EXPIRES (mirrored in rail.ts, pinned by the parity test): earliest-root-EVER left
  // an 8-day-old smoke probe wearing "brain" over a live team. A root counts while running or
  // recent; nothing current means no brain.
  const current = (r: RunLike) => r.status === "running"
    || nowSeconds === undefined || nowSeconds - (r.started_at ?? nowSeconds) < RECENT_SECONDS;
  const ours = runs.filter((r) =>
    r.status !== "foreign" && r.status !== "declared" && current(r));
  const ids = new Set(ours.map((r) => r.run_id));
  const roots = ours.filter((r) => !r.parent_run_id || !ids.has(r.parent_run_id));
  if (!roots.length) return null;
  return roots.reduce((first, r) =>
    (r.started_at ?? Infinity) < (first.started_at ?? Infinity)
    // A total order even on a tie: input order chose the brain before, and the crown moved
    // between renders.
    || ((r.started_at ?? Infinity) === (first.started_at ?? Infinity)
        && r.run_id.localeCompare(first.run_id) < 0) ? r : first).run_id;
}

/** What a run can do, or nothing.
 *
 *  A resolver that throws — a missing or moved definition file — degrades to no faculties rather
 *  than taking the whole workplace down. Claiming powers we couldn't verify would be worse than
 *  claiming none.
 */
function safeFaculties(run: RunLike, resolve: (run: RunLike) => string[]): string[] {
  try {
    return resolve(run) ?? [];
  } catch {
    return [];
  }
}

/** The registry's status as the FLOOR understands it.
 *
 *  Registry says running / done / failed / crashed / stopped / foreign; floor's stamp table
 *  knows running / done / error / foreign. This was a bare "as" cast, which silenced TypeScript
 *  over a real mismatch — a crashed agent arrived as "crashed", matched nothing, got NO stamp at
 *  all. Tree drew it red while the floor showed it as though nothing had happened, the worse of
 *  the two lies: a supervisor scanning the building for trouble saw none.
 *
 *  stopped reads as done rather than an error: somebody halted it deliberately, and stamping
 *  that as a failure would be the same kind of wrong in the other direction.
 */
export function floorStatus(status: string | undefined): Worker["status"] {
  // Declared in the company, never asked: READY reaches the world as itself, so the posture
  // table can draw it standing at ease — collapsing it to "done" made every never-asked agent a
  // lounger who looked like they had finished something.
  if (status === "declared") return "ready";
  switch (status) {
    case "failed":
    case "crashed":
      return "error";
    case "running":
    case "done":
    case "foreign":
      return status;
    case "stopped":
      return "done";
    default:
      return "done";
  }
}

export function buildTeam(
  runs: RunLike[],
  recentSteps: (runId: string) => Step[],
  now: number = Date.now() / 1000,
  messages: { from_run: string; to_run: string; text?: string; at?: number | null }[] = [],
  /** What a run can DO, resolved by the caller from its definition file. Injected rather than
   *  imported so this module stays free of the filesystem — and loadable by the test runner,
   *  which demands ".ts" specifiers tsc refuses to emit. Parsing lives at the edge in
   *  capabilities.ts; this only carries the answer. */
  facultiesFor: (run: RunLike) => string[] = () => [],
  /** The department an agent definition is filed under, resolved by the caller from the company
   *  file. Injected for the same reason as the faculties: this module reads no files. */
  departmentFor: (agent: string) => { id: string; room?: string | null } | null = () => null,
  /** WHO a run was held with — the role's stable id and what to call it.
   *
   *  World drew one body per RUN, so an agent given three errands stood in the room three times,
   *  every definition-less session rendering as another identical "claude". An agent is a
   *  PERSON; errands are what it was given. Injected for the same reason as the others: this
   *  module reads no files, and coordinator resolution lives in roster.ts. */
  identify: (run: RunLike) => { id: string; label: string } =
    (run) => ({ id: run.agent ?? run.name, label: run.name || run.run_id.slice(0, 8) }),
): TeamState {
  const brainId = brainOf(runs, now);
  // One body per AGENT. The errands stay with it as a count and as the run it opens; the roster
  // and the side panel are where you read them individually.
  const byAgent = new Map<string, RunLike[]>();
  for (const run of runs) {
    const { id } = identify(run);
    const held = byAgent.get(id);
    if (held) held.push(run); else byAgent.set(id, [run]);
  }
  const workers: Worker[] = [...byAgent.values()].map((held) => {
    // Two different questions, two different answers. WHICH errand the character opens is the
    // orchestrating one where this agent holds it (else rail and building would crown the same
    // agent through different runs); WHAT the character shows is whichever errand most needs
    // you — an agent with one failure and two successes isn't "done".
    // Only CURRENT errands speak, mirroring the rail's rule: a week-old crash must not stamp
    // ERROR over a character whose live errand is fine, and an agent whose every errand aged
    // out RESTS — the same "ready" its rail row shows, one truth on both surfaces.
    const terminal = new Set(["done", "stopped", "failed", "crashed"]);
    const currentHeld = held.filter((r) =>
      !(terminal.has(r.status) && now - (r.started_at ?? now) >= RECENT_SECONDS));
    const byNeed = [...(currentHeld.length ? currentHeld : held)].sort((a, b) =>
      NEEDS_YOU.indexOf(floorStatus(a.status)) - NEEDS_YOU.indexOf(floorStatus(b.status))
      || (b.started_at ?? 0) - (a.started_at ?? 0));
    const worst = byNeed[0];
    const rested = currentHeld.length === 0;
    const run = held.find((r) => r.run_id === brainId) ?? worst;
    const steps = recentSteps(run.run_id);
    const step = latestMeaningful(steps);
    // Idleness is time since the last thing this agent was OBSERVED doing — not time since it
    // started, which this used to measure. For a running agent finished_at is null, so the old
    // expression returned total elapsed; the view stamps HELD at two minutes and cuts the ambient
    // animation, so every agent working longer than that was drawn asleep — the harder it
    // worked, the deader the building looked.
    const lastSeen = lastObservedAt(steps);
    const since = run.status === "running"
      // No observation time (a record written before stamping existed) means unknown, and a false
      // "held" is worse than none — it puts a sleeping stamp on somebody who is working.
      ? lastSeen ?? now
      : run.finished_at ?? run.started_at ?? now;
    return {
      run_id: run.run_id,
      name: identify(run).label,
      /** How many CURRENT errands this agent holds — aged history lives in the roster's ledger. */
      tasks: held.every((r) => r.status === "declared") || rested ? 0 : currentHeld.length,
      agent: run.agent ?? null,
      status: rested ? "ready" : floorStatus(worst.status),
      faculties: safeFaculties(run, facultiesFor),
      brain: held.some((r) => r.run_id === brainId),
      ...placeByDomain(run.agent, departmentFor),
      // The whole window, not one step: a finished worker keeps the room it last worked in.
      zone: zoneOfSteps(steps, run.status, run.agent ?? null),
      // A session galaius didn't start gets named, never narrated: we don't read its stream,
      // and "waiting" would claim it is doing nothing when it is somebody working.
      activity: run.status === "foreign" ? "your own session" : activityOf(step),
      parent_run_id: run.parent_run_id ?? null,
      project: run.project ?? "",
      // Summed over EVERY errand this agent held, not the speaking run's alone: dropping the
      // rest made the map's team total disagree with the dashboard's on one screen.
      cost_usd: held.some((r) => r.cost_usd != null)
        ? held.reduce((sum, r) => sum + (r.cost_usd ?? 0), 0) : null,
      input_tokens: held.some((r) => r.input_tokens != null)
        ? held.reduce((sum, r) => sum + (r.input_tokens ?? 0), 0) : null,
      idle_seconds: Math.max(0, now - since),
      started_at: run.started_at ?? null,
      finished_at: run.finished_at ?? null,
    };
  });
  // Only exchanges between people actually in the room: a link to someone who has been forgotten
  // would be an arrow pointing at nobody.
  const present = new Set(workers.map((w) => w.run_id));
  const links: Link[] = messages
    .filter((m) => present.has(m.from_run) && present.has(m.to_run) && m.from_run !== m.to_run)
    .map((m) => ({
      from_run_id: m.from_run,
      to_run_id: m.to_run,
      text: (m.text ?? "").slice(0, 80),
      at: m.at ?? null,
    }));
  return { workers, links, at: now };
}
