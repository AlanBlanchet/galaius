/** A team, invented but shaped like a real one, so the view can be looked at before it is wired.
 *
 *  Chosen to hit the cases that actually break a layout rather than to look tidy: two leads with
 *  the SAME name in different projects (the pod colour is the only thing telling them apart), a
 *  report three levels down, reports scattered across four rooms away from their lead, an empty
 *  room, a stalled "running" worker, an errored one, and a session interact did not start.
 */
import { atSeconds } from "../palette";
import type { TeamState, Worker, ZoneId } from "../../../src/team";

type Seed = [
  name: string,
  agent: string | null,
  status: Worker["status"],
  zone: ZoneId,
  activity: string,
  parent: string | null,
  project: string,
  cost: number | null,
  idle: number,
  /** The department the company file files them under, and the faculties their own definition
   *  grants. Both taken from the REAL roster rather than invented: a fixture that exercises a
   *  shape the product doesn't use is not a fixture, it's an alibi — this project has been
   *  bitten by that four times (a unit, an id space, a baked clock, a synthetic image). */
  dept: string | null,
  faculties: string[],
];

/** The rooms exactly as ~/.claude/org.json words them, so the building's signs are the
 *  company's own and not a paraphrase. */
const ROOMS: Record<string, string> = {
  quality: "Quality & Critics",
  production: "Production & Makers",
  research: "Research & Intelligence",
  records: "Office of Records",
  finance: "Finance Desk",
};

const ALL = ["reads", "writes", "runs", "sees", "searches", "delegates"];

const SEEDS: Seed[] = [
  // Pod A — a session driving this very piece of work, its people spread over four rooms.
  ["main", null, "running", "managers", "delegating the workplace view to the artist", null, "interact", 1.94, 3, null, ALL],
  ["artist", "artist", "running", "studio", "drawing the pixel sprites for the team room", "a", "interact", 0.61, 1, "production", ["reads", "writes", "runs", "sees", "delegates"]],
  ["researcher", "researcher", "running", "web", "searching the web for pixel art css techniques", "a", "interact", 0.22, 0, "research", ["reads", "writes", "searches", "delegates"]],
  ["scraper", "scraper", "running", "web", "pulling the tilemap gallery", "c", "interact", 0.08, 6, "research", ["reads", "writes", "runs", "searches", "delegates"]],
  ["tester", "tester", "running", "lab", "running the webview suite", "a", "interact", 0.34, 12, "quality", ["reads", "writes", "runs", "delegates"]],
  ["Explore", "Explore", "done", "code", "read 14 files under webview/", "a", "interact", 0.05, 240, null, ["reads"]],
  ["librarian", "librarian", "done", "library", "synced paradigms/creative.md", "a", "interact", 0.41, 320, "records", ["reads", "writes", "runs", "delegates"]],

  // Pod B — another project entirely, same lead NAME, different colour.
  ["main", null, "running", "code", "editing crates/engine/src/kernels.rs", null, "any-compute", 0.77, 8, null, ALL],
  ["code-reviewer", "code-reviewer", "running", "code", "reviewing the diff against the ideology", "h", "any-compute", 0.19, 2, "quality", ["reads", "runs", "delegates"]],
  ["generalizer", "generalizer", "running", "code", "auditing the Tensor base class", "h", "any-compute", 0.16, 150, "quality", ["reads", "delegates"]],
  ["perf-critic", "perf-critic", "error", "lab", "benchmark harness died at p99", "h", "any-compute", 0.09, 61, "quality", ["reads", "runs", "sees", "delegates"]],

  // Pod C — a lead whose own parent has already been forgotten by the registry.
  ["visual-critic", "visual-critic", "running", "studio", "measuring contrast on the agents panel", null, "interact", 0.28, 4, "quality", ["reads", "runs", "sees", "delegates"]],
  ["ux-critic", "ux-critic", "running", "studio", "walking the surface graph from cold entry", "l", "interact", 0.12, 9, "quality", ["reads", "runs", "sees", "delegates"]],

  // Someone else's session, and someone who has finished and come back to the door.
  ["codex", null, "foreign", "idle", "", null, "unknown", null, 900, null, []],
  ["optimizer", "optimizer", "done", "entry", "handed the hot path back", null, "any-compute", 0.53, 44, "production", ["reads", "writes", "runs", "delegates"]],

  // The Finance Desk, which is the whole point of departments: finance work is filed elsewhere and
  // therefore stands in a room of its own, on the other side of the building from the critics.
  ["auditor", "auditor", "running", "data", "closing the books for the quarter", "a", "interact", 0.07, 5, "finance", ["reads"]],

  /* A DEPARTMENT WHERE EVERY RUN HAS FINISHED, which nothing in this fixture could show before.
     The whole claim of the posture system is that you read a room's condition off where its
     people ARE — desks or couches — with no word on screen, and a harness where no room is
     ever fully done can't put that claim in front of anybody. An independent critic hit exactly
     that and had to record the requirement as untestable — a hole in the fixture, not a gap in
     the feature: the state exists, the harness simply never entered it. */
  ["teacher", "teacher", "done", "library", "wrote the lesson for Conv2d", "a", "interact", 0.31, 190, "records", ["reads", "writes", "delegates"]],
  ["advocate", "advocate", "done", "library", "measured every claim in the README", "a", "interact", 0.24, 275, "records", ["reads", "writes", "runs"]],
];

const ID = "abcdefghijklmnopqrstuvwxyz";

export function fixture(at = Date.UTC(2026, 7, 18, 14, 3, 22)): TeamState {
  const workers: Worker[] = SEEDS.map(
    ([name, agent, status, zone, activity, parent, project, cost, idle, dept, faculties], i) => ({
      run_id: `run-${ID[i]}`,
      name,
      agent,
      status,
      zone,
      activity,
      parent_run_id: parent ? `run-${parent}` : null,
      project,
      cost_usd: cost,
      input_tokens: cost === null ? null : Math.round(cost * 42000),
      idle_seconds: idle,
      department: dept ?? undefined,
      room: dept ? ROOMS[dept] : undefined,
      // Exactly one brain: the first agent Alan asked for something. On the real roster that's
      // main, the run with no parent that started first.
      brain: i === 0,
      faculties,
    }),
  );
  // Real exchanges, both ends present. run-a is the first seed, run-b the second, and so on.
  //
  // Each carries an AGE in seconds, and the spread is the point: two happened moments ago and
  // three are old news. A cold open must act out the first two and stay silent about the rest,
  // and one link deliberately has NO stamp at all — the shape of a message recorded before
  // Link.at existed, which must count as unknown rather than as new.
  //
  // Stamps are in SECONDS because that's the unit the data layer actually writes (Python's
  // time.time()), while a snapshot's own at here is whatever the caller passed. Normalised
  // where they meet rather than assumed to agree — a fixture that quietly used one unit for
  // both is precisely what hid a 1970 clock in the panel for an entire arc.
  const said = (seconds: number) => atSeconds(at) - seconds;
  const links = [
    { from_run_id: "run-c", to_run_id: "run-b", text: "css: use box-shadow steps for the sprite, not a png", at: said(9) },
    { from_run_id: "run-b", to_run_id: "run-a", text: "sprites are in — need the zone list frozen", at: said(26) },
    { from_run_id: "run-j", to_run_id: "run-h", text: "Tensor base re-declares device in three leaves", at: said(640) },
    { from_run_id: "run-k", to_run_id: "run-h", text: "harness died at p99, re-running with a smaller batch", at: said(1800) },
    { from_run_id: "run-m", to_run_id: "run-l", text: "entry is reachable from cold, the panel is not" },
  ];
  return { workers, at, links } as TeamState;
}

/** The same team a moment later: three people have moved room and several have said something
 *  new. Rendering this straight after fixture() is how the walk is exercised — the view has to
 *  produce travel from the difference alone, with nothing telling it who moved. */
export function fixtureMoved(at = Date.UTC(2026, 7, 18, 14, 3, 31)): TeamState {
  const state = fixture(at);
  const move = (name: string, zone: ZoneId, activity: string) => {
    const w = state.workers.find((x) => x.name === name);
    if (w) {
      w.zone = zone;
      w.activity = activity;
      w.idle_seconds = 0;
    }
  };
  // Two exchanges that did NOT exist a moment ago: the couriers must carry exactly these, and
  // must not re-carry the five that were already on screen.
  const now = atSeconds(at);
  (state as { links: { from_run_id: string; to_run_id: string; text: string; at?: number }[] }).links.push(
    { from_run_id: "run-a", to_run_id: "run-l", text: "can you look at the workplace panel when it lands?", at: now - 2 },
    { from_run_id: "run-h", to_run_id: "run-a", text: "kernels.rs is green, moving to the reduce path", at: now - 1 },
  );
  move("researcher", "managers", "reporting back what the web had");
  move("artist", "code", "writing webview/workplace/scene.ts");
  move("tester", "entry", "done — suite is green");
  move("scraper", "data", "filing the gallery into the cache");
  const t = state.workers.find((x) => x.name === "tester");
  if (t) t.status = "done";
  return state;
}
