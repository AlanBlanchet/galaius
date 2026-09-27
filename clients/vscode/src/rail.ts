/** The rail: what the panel shows at rest, decided without a vscode import.
 *
 *  Chosen replacement for the tree + chat split. Must be a webview rather than a TreeView for a
 *  measured reason, not a stylistic one: VS Code hides a view's title actions until the pointer
 *  enters the header (.pane-header > .actions { display: none }) and CLIPS overflow with no "…"
 *  menu to recover from. A resting panel rendered ZERO buttons, and the team view — the thing
 *  most worth reaching — could only be found by hovering and guessing among unlabelled glyphs. No
 *  arrangement of icons fixes that inside a tree, at any width. A webview draws its own chrome,
 *  which can simply be visible.
 *
 *  Two decisions live here, making this a SUPERVISION surface rather than a list:
 *
 *  - roster ordered by WHO NEEDS YOU, not identity. With a couple dozen seats and ten runs,
 *    alphabetical order buries the one thing that went wrong somewhere in the middle;
 *  - destinations are LABELLED. An icon you have to hover to identify is not a destination, it's
 *    a guess.
 */

import type { AgentRun } from "./agents";

/** How long a running agent may say nothing before it's worth your attention — the same
 *  threshold the workplace uses for a stalled worker, one number, or the two surfaces disagree
 *  about the word "stuck". Mirrors HELD_AFTER_SECONDS in statusLanguage.ts, the rule's home.
 *
 *  Duplicated rather than imported for one mechanical reason: this module is loaded DIRECTLY by
 *  node --test, which can't resolve an extensionless sibling import, and tsc won't emit the .ts
 *  specifier that loader needs. So the value is mirrored and EQUALITY enforced by a test — the
 *  divergence that actually hurts is the roster calling someone stuck while the world still
 *  shows them working, not the second const.
 */
export const HELD_SECONDS = 120;

/** Why a run wants attention, worst first. The ORDER of this list IS the sort. */
export type Attention = "error" | "asked" | "held" | "finished" | "working" | "not-ours" | "ready" | "stopped";

export const RANK: Attention[] = ["error", "asked", "held", "stopped", "working", "finished", "ready", "not-ours"];

export interface RailRun {
  run: AgentRun;
  attention: Attention;
  /** The orchestrator — the first agent you asked, which put the others to work. Marked rather
   *  than pinned to the top: this surface sorts by who NEEDS you, and a healthy boss must never
   *  bury a crashed agent. */
  brain: boolean;
  /** How many errands this row stands for. Above 1 only at the top level, where a row is an
   *  AGENT — the same unit the map draws — and the worst of its errands speaks for it. */
  tasks?: number;
  /** The agent's WHOLE bill in scope — every errand, current and aged. Same figure the world
   *  badges: the speaking run's own cost beside the world's sum was "one panel, two truths" —
   *  code-reviewer read $2.31 in the rail, $4.28 in the room. */
  cost?: number;
  /** 0 for a lead, 1 for somebody a lead sent out. The rail cannot replace the tree until it
   *  shows the COMPANY rather than a flat list — a sub-agent floating loose beside its lead tells
   *  you nothing about who is driving what. */
  depth: number;
  /** What the row says about its state, in the reader's words rather than the registry's. */
  note: string;
}

/** What a run needs from you right now.
 *
 *  foreign sorts last deliberately: one of your own editor sessions is not the team's work, and
 *  you can't act on it from here, so it must never outrank an agent that actually stopped.
 */
export function attentionOf(run: AgentRun, idleSeconds: number, awaitingReply = false): Attention {
  if (run.status === "foreign") return "not-ours";
  // Declared in the company file, never asked: present and quiet, below everything with history.
  if (run.status === "declared") return "ready";
  if (run.status === "failed" || run.status === "crashed") return "error";
  // Killed by a person: never success, mildly attention-worthy — the kill had a reason.
  if (run.status === "stopped") return "stopped";
  if (run.status === "running") {
    if (awaitingReply) return "asked";
    return idleSeconds >= HELD_SECONDS ? "held" : "working";
  }
  // done / stopped: finished work is worth seeing once, then it should stop competing.
  return "finished";
}

/** How long finished work keeps its seat before it becomes history. A day: yesterday's finish is
 *  still news, last week's smoke test is not — and the owner's own panel proved it, resting on
 *  seven 8-day-old "Reply with exactly:" probes rendered as the team. */
export const RECENT_SECONDS = 86400;

/** Old terminal work, folded to one line. The runs are still here — the ledger OPENS — but they
 *  no longer compete with the living team for rows. */
export interface RailLedger {
  runs: RailRun[];
  done: number;
  stopped: number;
  failed: number;
  cost: number;
}

export interface RailChip {
  id: string;
  label: string;
  command: string;
}

/** The destinations — always visible, always named, deliberately few. Old title bar carried
 *  seven actions and clipped the seventh at every width; the fix isn't a smaller icon, it's
 *  fewer destinations with words on them.
 *
 *  Two, not four. "The sidepanel is there to view info about who we click on, and view the
 *  conversation... That's all." Dashboard and sequence view are their own surfaces, staying in
 *  the command palette; a 299px column whose job is the roster and the reply shouldn't spend a
 *  fifth of its header as a launcher for them. What survives: the world you watch, and the way
 *  to start someone new — the two things you can't do anywhere else.
 */
export const CHIPS: RailChip[] = [
  { id: "team", label: "Team", command: "interact.agents.team" },
  /* "I should always be able to create a new session." A session is the DEFAULT way in: the
     entry agent takes the brief and puts specialists to work, exactly as a Claude Code session
     does. Picking one of forty specialists yourself is a staffing decision, and it stays — one
     step over, honestly labelled — for when that is genuinely what you mean. */
  { id: "session", label: "+ Session", command: "interact.agents.newSession" },
  { id: "company", label: "Staff", command: "interact.agents.spawn" },
];

export interface RailHeader {
  /** The workspace being shown — the answer to "whose agents are these?", which the tree only
   *  ever gave you inside a dialog you had to know to open. */
  scope: string;
  working: number;
  needsYou: number;
  finished: number;
}

export interface Rail {
  header: RailHeader;
  chips: RailChip[];
  /** The working list — what is happening NOW. Old finishes move to the ledger, resting
   *  colleagues to the staff band, your own sessions to theirs: four bands, so the first
   *  thing the eye lands on is the living work, never a week-old smoke test. */
  runs: RailRun[];
  /** Declared colleagues with nothing current — the company at rest, named and quiet. */
  staff?: RailRun[];
  /** Your OWN editor sessions — the real work this machine is doing right now. Dropped entirely
   *  once ("agents that are greyed out"), which was half right: they don't belong INTERLEAVED
   *  with the team as grey dead rows. But hiding them hid the company — "I don't have a view
   *  just like in claude code... of agent building things" — so they stand in their own band,
   *  titled by project, each opening its transcript. */
  yours?: RailRun[];
  /** Terminal work older than RECENT_SECONDS, folded to one expandable line. */
  ledger?: RailLedger | null;
  /** The agent whose conversations you are looking at, if you have gone into one. An agent is a
   *  ROLE — you can hold many conversations with it — so "inside tester" is a real place in this
   *  panel, and the breadcrumb is how you leave it. Absent means the whole team. */
  filter?: string;
}

const NOTES: Record<Attention, string> = {
  ready: "ready",
  stopped: "stopped",
  error: "stopped with an error",
  asked: "asked you something",
  held: "nothing for a while",
  finished: "finished",
  working: "working",
  "not-ours": "your own session",
};

/** The orchestrator: the earliest ROOT run that's ours — the first agent you asked for
 *  something, which then put the others to work.
 *
 *  DUPLICATED from teamState.ts on purpose, pinned by a test that runs both against the same
 *  input. Neither module can import the other: both load directly under the test runner, which
 *  demands ".ts" specifiers tsc refuses to emit, so a shared import would make one untestable.
 *  Six lines copied beats a module that can't be tested.
 */
export function brainOf(runs: readonly AgentRun[], nowSeconds?: number): string | null {
  // The crown EXPIRES. Earliest-root-ever left an 8-day-old smoke probe wearing "the agent you
  // asked" over a live team; the orchestrator of the CURRENT arc is a root that is running or
  // recent. With nothing current there is no brain — an empty office has no boss on duty.
  const current = (r: AgentRun) => r.status === "running"
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

/** The whole rail, from the runs in scope.
 *
 *  idleOf and awaitingReply are passed in rather than read here, so this module stays free of
 *  the filesystem and of vscode — same discipline as agentsFormat.ts, letting every decision
 *  below be tested without an extension host.
 */
export function buildRail(
  runs: readonly AgentRun[],
  scope: string,
  idleOf: (run: AgentRun) => number,
  awaitingReply: (run: AgentRun) => boolean = () => false,
  /** Narrow to one agent's conversations. Applied FIRST, so counts, leads and reports all
   *  describe the thing you're actually looking at, not the team behind it. */
  filter?: { agent: string; roleOf: (run: AgentRun) => string },
  /** WHO holds each run. When present and NOT drilled in, the roster GROUPS: one row per agent,
   *  exactly the unit the map draws — the cold sweep's root coherence finding was one sprite per
   *  role beside sixteen run-rows, nothing reconciling them. */
  identify?: (run: AgentRun) => { id: string; label: string },
  /** The clock, when the host wants HISTORY folded away: terminal runs older than
   *  RECENT_SECONDS leave the list for the ledger. Absent (tests, hosts that want it all),
   *  nothing ages — see RECENT_SECONDS above for why that matters. */
  nowSeconds?: number,
): Rail {
  // Own editor sessions get their own band, not interleaved with the team — see Rail.yours.
  const yoursRuns = filter ? [] : runs.filter((r) => r.status === "foreign");
  const held = runs.filter((r) => r.status !== "foreign");
  const inScope = filter ? held.filter((r) => filter.roleOf(r) === filter.agent) : held;
  // Old TERMINAL work stops competing for rows. Never inside a drill-in: an agent's own page is
  // where you asked for its history.
  const terminal = new Set(["done", "stopped", "failed", "crashed"]);
  const ancient = (r: AgentRun) =>
    nowSeconds !== undefined && !filter && terminal.has(r.status)
    && nowSeconds - (r.started_at ?? nowSeconds) >= RECENT_SECONDS;
  const aged = inScope.filter(ancient);
  const fresh = inScope.filter((r) => !ancient(r));
  let rows: RailRun[];
  if (identify && !filter) {
    // One row per AGENT. The errand that most needs him speaks for the row (same rule as the
    // map's characters); count carries the rest, drilling in lists them individually. Only
    // CURRENT errands speak — a week-old crash must not outshout today's work.
    const byAgent = new Map<string, AgentRun[]>();
    for (const run of fresh) {
      const { id } = identify(run);
      const bucket = byAgent.get(id);
      if (bucket) bucket.push(run); else byAgent.set(id, [run]);
    }
    // The bill covers EVERYTHING in scope, aged errands included — the exact sum the world
    // badges on the character, so the two surfaces state one figure.
    const billOf = new Map<string, number | undefined>();
    for (const run of inScope) {
      const { id } = identify(run);
      if (run.cost_usd == null) { if (!billOf.has(id)) billOf.set(id, undefined); continue; }
      billOf.set(id, (billOf.get(id) ?? 0) + run.cost_usd);
    }
    rows = [...byAgent.entries()].map(([id, bucket]) => {
      const speaks = [...bucket].sort((a, b) => {
        const rank = (r: AgentRun) => RANK.indexOf(attentionOf(r, idleOf(r), awaitingReply(r)));
        return rank(a) - rank(b) || (b.started_at ?? 0) - (a.started_at ?? 0);
      })[0];
      const attention = attentionOf(speaks, idleOf(speaks), awaitingReply(speaks));
      return { run: speaks, attention, depth: 0, brain: false, note: NOTES[attention],
               tasks: bucket.every((r) => r.status === "declared") ? 0 : bucket.length,
               cost: billOf.get(id) };
    });
    // An agent whose EVERY errand aged out still exists — it rests as staff, named, rather than
    // wearing an 8-day-old task as its face. Its newest errand supplies the identity.
    const alive = new Set(fresh.map((r) => identify(r).id));
    const rested = new Map<string, AgentRun>();
    for (const run of aged) {
      const { id } = identify(run);
      if (alive.has(id)) continue;
      const seen = rested.get(id);
      if (!seen || (run.started_at ?? 0) > (seen.started_at ?? 0)) rested.set(id, run);
    }
    for (const run of rested.values()) {
      rows.push({ run, attention: "ready", depth: 0, brain: false, note: NOTES.ready, tasks: 0 });
    }
  } else {
    rows = fresh.map((run) => {
      const attention = attentionOf(run, idleOf(run), awaitingReply(run));
      return { run, attention, depth: 0, brain: false, note: NOTES[attention] };
    });
  }

  const byAttention = (a: RailRun, b: RailRun) => {
    const byRank = RANK.indexOf(a.attention) - RANK.indexOf(b.attention);
    if (byRank !== 0) return byRank;
    // Within a band, most recently started first: the newest error is the one you have not seen.
    return (b.run.started_at ?? 0) - (a.run.started_at ?? 0);
  };

  // Leads first, each followed immediately by the people it sent out. A run whose lead is not in
  // the list — its parent finished and was cleared — is a lead again rather than dropped, because
  // hiding a live agent is the one thing a roster must never do.
  const present = new Set(rows.map((r) => r.run.run_id));
  const isLead = (r: RailRun) =>
    !r.run.parent_run_id || !present.has(r.run.parent_run_id);
  const reportsOf = new Map<string, RailRun[]>();
  for (const row of rows.filter((r) => !isLead(r))) {
    const under = reportsOf.get(row.run.parent_run_id!) ?? [];
    under.push(row);
    reportsOf.set(row.run.parent_run_id!, under);
  }

  const brainId = brainOf(runs, nowSeconds);
  // Leads sort by ATTENTION, not rank. Pinning the brain to the top was tried and reverted — it
  // contradicts what this surface is for: an agent that crashed outranks the orchestrator
  // quietly working, and burying a crash under a healthy boss is the sort this replaced. Brain
  // is MARKED instead, findable without displacing what needs you.
  const leads = rows.filter(isLead).sort(byAttention);

  const ordered: RailRun[] = [];
  for (const lead of leads) {
    ordered.push({ ...lead, brain: lead.run.run_id === brainId });
    for (const report of (reportsOf.get(lead.run.run_id) ?? []).sort(byAttention)) {
      ordered.push({ ...report, depth: 1 });
    }
  }
  rows.length = 0;
  rows.push(...ordered);

  // The bands. Inside a drill-in there is exactly one: the agent's full history.
  const labelOf = (r: RailRun) => identify?.(r.run).label ?? r.run.name;
  const staff = filter ? [] : rows.filter((r) => r.attention === "ready")
    .sort((a, b) => labelOf(a).localeCompare(labelOf(b)));
  const now = filter ? rows : rows.filter((r) => r.attention !== "ready");
  const yours: RailRun[] = yoursRuns
    .map((run) => ({ run, attention: "not-ours" as Attention, depth: 0, brain: false,
                     note: NOTES["not-ours"] }))
    .sort((a, b) => (b.run.started_at ?? 0) - (a.run.started_at ?? 0));
  const agedRows: RailRun[] = aged
    .map((run) => {
      const attention = attentionOf(run, idleOf(run), awaitingReply(run));
      return { run, attention, depth: 0, brain: false, note: NOTES[attention] };
    })
    .sort((a, b) => (b.run.started_at ?? 0) - (a.run.started_at ?? 0));
  const ledger: RailLedger | null = agedRows.length ? {
    runs: agedRows,
    done: agedRows.filter((r) => r.attention === "finished").length,
    stopped: agedRows.filter((r) => r.attention === "stopped").length,
    failed: agedRows.filter((r) => r.attention === "error").length,
    cost: agedRows.reduce((sum, r) => sum + (r.run.cost_usd ?? 0), 0),
  } : null;

  const count = (...kinds: Attention[]) => now.filter((r) => kinds.includes(r.attention)).length;

  return {
    header: {
      scope,
      working: count("working"),
      // What the header exists for: one number saying whether anything wants you. A count of
      // everything running answers "is it busy", which is never the question being asked.
      needsYou: count("error", "asked", "held"),
      finished: count("finished"),
    },
    chips: CHIPS,
    runs: now,
    staff,
    yours,
    ledger,
    filter: filter?.agent,
  };
}

/** The commands a ROW may run. Mirrors agentActions.ts and is pinned to it by a test — the two
 *  can't import each other under the test loader, and an allowlist drifting from the buttons
 *  either breaks a control or admits one nobody offered. */
const ROW_COMMANDS = new Set([
  "interact.agents.send",
  "interact.agents.show",
  "interact.agents.openConversation",
  "interact.agents.showEvents",
  "interact.agents.stop",
  "interact.agents.model",
  // Opens the definition FILE — "See their instructions" used to open the transcript, a label
  // the professional sweep caught lying.
  "interact.agents.definition",
]);

export type RailAction =
  | { kind: "command"; command: string }
  | { kind: "open"; runId: string }
  /** A per-row action: run this command AGAINST this agent. Carries the run id because the
   *  commands it names all operate on one agent, and the panel must not act on whichever
   *  happened to be selected. */
  | { kind: "act"; command: string; runId: string }
  /** Go to an agent's conversations, or back to the whole team when the id is empty. */
  | { kind: "agent"; id: string | null };

/** Changing which project you are looking at. Not a destination chip — the chips are capped to
 *  what fits a narrow sidebar, and this belongs on the scope label itself, which is the thing that
 *  states the answer it changes. Allow-listed here so the webview can ask for it by name. */
export const SCOPE_COMMAND = "interact.agents.workspace";

/** What the webview asked for, or null.
 *
 *  The rail renders agent output, so every message arriving FROM it is untrusted. A command is
 *  accepted only if the rail actually OFFERS it — not because it's one of ours or because it
 *  exists: running an arbitrary command id because a message named it is a real hole, and the
 *  chat view already validates the same way. Anything unrecognised produces null, which the
 *  caller ignores; guessing at a malformed message is how a webview becomes an exec surface.
 */
export function railAction(message: unknown): RailAction | null {
  if (!message || typeof message !== "object") return null;
  const msg = message as { type?: unknown; command?: unknown; runId?: unknown };
  if (msg.type === "agent") {
    const id = typeof (msg as { id?: unknown }).id === "string" ? (msg as { id: string }).id : "";
    return { kind: "agent", id: id || null };
  }
  if (msg.type === "command" && typeof msg.command === "string") {
    const offered = CHIPS.some((c) => c.command === msg.command) || msg.command === SCOPE_COMMAND
      || msg.command === "interact.openDashboard";
    return offered ? { kind: "command", command: msg.command } : null;
  }
  if (msg.type === "open" && typeof msg.runId === "string" && msg.runId) {
    return { kind: "open", runId: msg.runId };
  }
  if (msg.type === "act" && typeof msg.command === "string" && typeof msg.runId === "string"
      && msg.runId) {
    // Checked against what a row may offer — the same rule as the chips, for the same reason:
    // this surface renders agent output, so "it is a real command" is not the test.
    return ROW_COMMANDS.has(msg.command)
      ? { kind: "act", command: msg.command, runId: msg.runId } : null;
  }
  return null;
}

/** What to do with a message from the webview.
 *
 *  Lives here beside the validation rather than in the provider, so the part that can silently
 *  execute the wrong thing is testable without an extension host — the provider imports vscode
 *  and can't be loaded by the test runner at all.
 */
export interface RailHandlers {
  run: (command: string) => void;
  open: (runId: string) => void;
  act: (command: string, runId: string) => void;
  /** Go to one agent's conversations, or back to the whole team when null. Optional so a host that
   *  does not offer the drill-in simply does not wire it, rather than crashing on a message. */
  agent?: (id: string | null) => void;
}

export function railRoute(message: unknown, handlers: RailHandlers): void {
  const action = railAction(message);
  if (!action) return;
  if (action.kind === "command") handlers.run(action.command);
  else if (action.kind === "act") handlers.act(action.command, action.runId);
  else if (action.kind === "agent") handlers.agent?.(action.id);
  else handlers.open(action.runId);
}
