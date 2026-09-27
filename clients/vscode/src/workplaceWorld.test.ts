/** The building is now a TILE MAP solved from the company's departments, so what is worth
 *  guarding changed with it.
 *
 *  The old guard here asserted `data-vacant` on a flexbox storey; those storeys no longer exist,
 *  and a test that asserts an absent selector does not fail, it ERRORS — which is how the
 *  contrast guard beside it went silently missing while still looking present. So these assert
 *  the properties the tile world actually rests on, all of which have already been broken once
 *  during this build:
 *
 *   - every department the roster names gets a room, and nobody is placed in a room the company
 *     never declared;
 *   - a room is SIZED to its headcount, because a fixed room silently stacks the extra people on
 *     top of each other (six critics were drawn as one pile);
 *   - every seat is WALKABLE — a seat generated onto a wall tile put a body inside the masonry,
 *     which happened the first time the bays were made variable;
 *   - every room is REACHABLE from every other one through the doors, because a body that cannot
 *     path to its own desk simply never moves, and a still character is indistinguishable from
 *     a still view.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createRequire } from "node:module";

import type { TeamState, Worker } from "./team.ts";

const wp = createRequire(import.meta.url)("../out/workplace.js");
const worldFor = wp.worldFor as (workers: readonly Worker[]) => World;
const placeOf = wp.placeOf as (world: World, w: Worker) => Room;
const renderScene = wp.renderScene as (s: TeamState) => string;

interface Room {
  id: string;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
  brain: boolean;
  outdoor: boolean;
  seats: { x: number; y: number }[];
}
interface World {
  cols: number;
  rows: number;
  rooms: Room[];
  solid: boolean[];
  facadeX: number;
  gateY: number;
  brainRoom: Room;
  lobby: Room;
  yard: Room;
}

let n = 0;
function person(over: Partial<Worker> = {}): Worker {
  n += 1;
  return {
    run_id: `run-${n}`,
    name: `w${n}`,
    agent: null,
    status: "running",
    zone: "code",
    activity: "",
    parent_run_id: null,
    project: "p",
    cost_usd: null,
    input_tokens: null,
    idle_seconds: 0,
    ...over,
  } as Worker;
}

const team = (workers: Worker[]): TeamState => ({ workers, links: [], at: 0 }) as TeamState;

/** Can a body get from one tile to another through the doors? The same breadth-first walk the
 *  engine does, so this is the routing graph and not a proxy for it. */
function reachable(w: World, from: { x: number; y: number }, to: { x: number; y: number }): boolean {
  const seen = new Set<number>();
  const q = [from.y * w.cols + from.x];
  const goal = to.y * w.cols + to.x;
  seen.add(q[0]);
  for (let head = 0; head < q.length; head++) {
    const cur = q[head];
    if (cur === goal) return true;
    const cx = cur % w.cols;
    const cy = Math.floor(cur / w.cols);
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = cx + dx;
      const ny = cy + dy;
      if (nx < 0 || ny < 0 || nx >= w.cols || ny >= w.rows) continue;
      const i = ny * w.cols + nx;
      if (w.solid[i] || seen.has(i)) continue;
      seen.add(i);
      q.push(i);
    }
  }
  return false;
}

const ROSTER = [
  person({ department: "quality", room: "Quality & Critics" }),
  person({ department: "quality", room: "Quality & Critics" }),
  person({ department: "production", room: "Production & Makers" }),
  person({ department: "finance", room: "Finance Desk" }),
  person({ brain: true } as Partial<Worker>),
  person({}), // filed under nothing
  person({ zone: "web" }),
];

test("every department the roster names becomes a room, and none is invented", () => {
  const w = worldFor(ROSTER);
  const ids = w.rooms.map((r) => r.id);
  for (const dept of ["quality", "production", "finance"]) {
    assert.ok(ids.includes(dept), `no room for ${dept}`);
  }
  assert.ok(!ids.includes("research"), "a department nobody is in must not get a room");
  assert.equal(w.rooms.filter((r) => r.brain).length, 1, "exactly one brain room");
});

test("a worker stands in their own department, and only the unfiled use the lobby", () => {
  const w = worldFor(ROSTER);
  assert.equal(placeOf(w, ROSTER[0]).id, "quality");
  assert.equal(placeOf(w, ROSTER[3]).id, "finance");
  assert.equal(placeOf(w, ROSTER[4]).id, w.brainRoom.id, "the brain stands in the middle");
  assert.equal(placeOf(w, ROSTER[5]).id, w.lobby.id, "no department, so the front desk");
  assert.ok(placeOf(w, ROSTER[6]).outdoor, "the web is genuinely outside the building");
});

test("a room is as wide as its people — a fixed room stacks them on top of each other", () => {
  const small = worldFor([person({ department: "d", room: "D" })]);
  const crowd = worldFor(
    Array.from({ length: 9 }, () => person({ department: "d", room: "D" })),
  );
  const a = small.rooms.find((r) => r.id === "d")!;
  const b = crowd.rooms.find((r) => r.id === "d")!;
  assert.ok(b.w > a.w, `nine people got the same ${a.w}-tile room as one`);
  assert.ok(b.seats.length >= 9, `nine people, ${b.seats.length} standing places`);
});

test("every seat in the building is a tile a body may actually stand on", () => {
  const w = worldFor(ROSTER);
  const bad: string[] = [];
  for (const room of w.rooms) {
    for (const s of room.seats) {
      if (s.x < 0 || s.y < 0 || s.x >= w.cols || s.y >= w.rows) bad.push(`${room.id} ${s.x},${s.y} off-map`);
      else if (w.solid[s.y * w.cols + s.x]) bad.push(`${room.id} ${s.x},${s.y} is solid`);
    }
  }
  assert.deepEqual(bad, []);
});

test("every room can be walked to from every other one, and the yard through the gate", () => {
  const w = worldFor(ROSTER);
  const first = w.rooms[0].seats[0];
  const unreachable = w.rooms
    .filter((r) => r.seats.length)
    .filter((r) => !reachable(w, first, r.seats[0]))
    .map((r) => r.id || "(lobby)");
  assert.deepEqual(unreachable, [], "a body that cannot path to its own desk never moves");
});

test("an empty team still renders a building rather than nothing", () => {
  const html = renderScene(team([]));
  assert.match(html, /class="wp-map"/);
  assert.ok(html.includes('data-room="__brain"'), "the brain room exists with nobody in it");
  assert.match(html, /class="wp-world"/, "the simulation still gets its walkability grid");
});

test("the scene hands the engine a grid whose size matches the map it drew", () => {
  const html = renderScene(team(ROSTER));
  const cols = Number(/data-cols="(\d+)"/.exec(html)![1]);
  const rows = Number(/data-rows="(\d+)"/.exec(html)![1]);
  const solid = /data-solid="([01]+)"/.exec(html)![1];
  assert.equal(solid.length, cols * rows, "the walkability grid is a different size from the map");
});

test("a production lead and its launched agent expose lineage without relying on pod colour", () => {
  const lead = person({
    run_id: "production-root",
    name: "implementation lead",
    department: "production",
    room: "Production & Makers",
  });
  const child = person({
    run_id: "production-child",
    name: "frontend",
    parent_run_id: lead.run_id,
    department: "production",
    room: "Production & Makers",
  });
  const html = renderScene(team([lead, child]));
  const root = html.slice(html.indexOf('data-run-id="production-root"'));
  const launched = html.slice(html.indexOf('data-run-id="production-child"'));

  assert.match(root, /data-lineage="root"/,
    "the main agent needs a typed, non-colour relationship in the shipped markup");
  assert.match(root, /aria-label="[^"]*main agent/i,
    "screen-reader users must hear which actor is the main agent");
  assert.match(launched, /data-lineage="child"/,
    "a launched agent needs a typed relationship, not only a shared pod colour");
  assert.match(launched, /aria-label="[^"]*launched by implementation lead/i,
    "the child actor must name its parent in the accessible description");
});
