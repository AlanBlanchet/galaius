/** The TEAM as a workplace — the shape both data layer and view are built to.
 *
 *  The panel so far answered "which runs exist". This answers "what is my team DOING right now":
 *  each worker stands in the ZONE matching the work in flight — web, code, library — so a glance
 *  reads as a room full of people, not a list of rows.
 *
 *  CONTRACT only: no rendering, no I/O, so view and data can be built at the same time without
 *  drifting.
 */

/** Where a worker stands. Zones are places, not tool categories: AT the library, not that they
 *  called a read tool. */
export type ZoneId =
  | "entry" // where a request arrives and where a finished worker returns
  | "managers" // orchestrating: spawning, delegating, waiting on others
  | "code" // reading and writing the source
  | "data" // the registry, caches, the stores on disk
  | "web" // out of the building: research, fetching, the world
  | "library" // the prompts, the definitions, the docs
  | "studio" // anything visual — screenshots, driving an app, judging pixels
  | "lab" // tests, measurements, verification
  | "idle"; // present but not working

export interface Zone {
  id: ZoneId;
  /** What the sign on the door says. */
  label: string;
}

/** Ordered as a workplace map lays out: door first, back rooms last. */
export const ZONES: Zone[] = [
  { id: "entry", label: "Entry" },
  { id: "managers", label: "Managers" },
  { id: "code", label: "Code" },
  { id: "data", label: "Data" },
  { id: "web", label: "Web" },
  { id: "library", label: "Library" },
  { id: "studio", label: "Studio" },
  { id: "lab", label: "Lab" },
  { id: "idle", label: "Break room" },
];

export interface Worker {
  run_id: string;
  /** The name on the badge — the agent definition where there is one. */
  name: string;
  agent: string | null;
  /** How many errands this agent was given. One body stands for all of them. */
  tasks?: number;
  status: "running" | "done" | "error" | "foreign"
    /** Declared in the company, never asked. Drawn STANDING at ease (crisp, no fade, waiting for
     *  its first job) vs finished LOUNGES having done one — different postures; artist owns the
     *  posture table. */
    | "ready";
  zone: ZoneId;
  /** What they are doing, in words a person would say: "reading registry.py". */
  activity: string;
  /** Who sent them. A sub-agent stands with its parent, in the parent's own context. */
  parent_run_id: string | null;
  project: string;
  cost_usd: number | null;
  input_tokens: number | null;
  /** Seconds since last activity — fades the ones who've stopped. */
  idle_seconds: number;
  /** Registry's own clock, carried so no view invents one. Optional: a fixture or old record may
   *  lack it — never assume a clock exists. */
  started_at?: number | null;
  finished_at?: number | null;
  /** DOMAIN this one belongs to — the department its definition is filed under (quality,
   *  production, research, records, finance). Org already declares rooms per department, but
   *  placement ignored it, so a finance agent stood with a code reviewer. Absent when the
   *  definition names no department: an unplaced character beats a silently wrong room. */
  department?: string;
  /** That department's room name, as the company file words it ("Finance Desk"). */
  room?: string;
  /** The orchestrator: the first agent YOU asked, which put the others to work. Exactly one per
   *  team, never one of your own editor sessions — galaius doesn't drive those, so crowning one
   *  would claim authority the view lacks. Without it every character ranked the same, so the
   *  building read as a bag of sprites, not a company. */
  brain?: boolean;
  /** What this one can actually DO, inherited from its definition file — reads / writes / runs /
   *  sees / searches / delegates. Empty for a plain run with no definition: inventing a power
   *  would misreport what's loose in your workspace. See capabilities.ts. */
  faculties?: string[];
}

/** One agent addressing another — what makes a set of workers a TEAM, not a set of processes.
 *  Drawn between the rooms the two ends stand in. */
export interface Link {
  /** When this exchange happened. Without it, a view can't tell "just spoke" from "an hour ago",
   *  so on cold open it stays silent — half of "no agents talk to each other". Optional: absent
   *  on messages recorded before stamping existed. */
  at?: number | null;
  from_run_id: string;
  to_run_id: string;
  /** What was said, clipped — enough to read the exchange, not the whole message. */
  text: string;
}

export interface TeamState {
  workers: Worker[];
  /** Who has spoken to whom. Optional — without it the room shows people standing in it, not a
   *  team working together. */
  links?: Link[];
  /** Wall-clock of the snapshot (SECONDS since epoch, matching Python's time.time()) — the unit
   *  every timestamp in this state uses (started_at, finished_at, Link.at, AgentActivity.at). So
   *  the view can age what it draws.
   *
   *  Spelled out because it wasn't, and a bare number cost a real bug: the view read it with new
   *  Date(state.at), which wants MILLISECONDS, rendering a 1970 clock. It survived a whole arc
   *  because dev fixtures stamped Date.UTC(...) — already milliseconds — so the harness looked
   *  right while the product showed 1970.
   */
  at: number;
}

/** Workers that answer to nobody — the top of each tree. */
export function leads(workers: Worker[]): Worker[] {
  const ids = new Set(workers.map((w) => w.run_id));
  return workers.filter((w) => !w.parent_run_id || !ids.has(w.parent_run_id));
}

/** Who this worker sent out. A sub-agent belongs beside its parent, not loose in the room. */
export function reportsOf(workers: Worker[], runId: string): Worker[] {
  return workers.filter((w) => w.parent_run_id === runId);
}
