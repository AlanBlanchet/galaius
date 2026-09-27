/** WHERE PEOPLE STAND, checked against the building rather than against a screenshot.
 *
 *  "Sprites are very weirdly placed, and are just simply bad."
 *
 *  That defect had no visible symptom worth the name. Everybody was on the floor, nobody was in a
 *  wall, and every screenshot looked like a room with people in it — the placement was simply
 *  MEANINGLESS: two ranks that collapsed onto the same row in any depth-6 room, an overflow rule
 *  that pushed the extra bodies one row NORTH onto the desks they were supposed to be working at,
 *  and a "rest" rank standing three tiles from the nearest thing to sit on. None of it throws, so
 *  none of it shows up in a test that renders strings, and by the time it is visible in pixels it
 *  is one frame of a simulation that is always moving.
 *
 *  So the rule is stated as an invariant over the built world instead, and it is one line:
 *
 *      A PLACE IS DEFINED BY WHAT IS BEHIND IT.
 *
 *  Every standing place that claims something to sit on must have that something on the tile
 *  directly north of it — a desk for a working place, a couch or a bench for a resting one. A
 *  place that has nothing declares `perch: false` and its occupant is drawn standing. There is no
 *  third case, and a seat that cannot say which it is is the bug.
 *
 *  Prints one line per room and exits non-zero on any violation, so it can be run by hand or by
 *  the suite:
 *
 *      node out-placement.js
 */
import { brainOf, deptsOf, placeOf, seating, worldFor } from "../scene";
import { PITCH } from "../world";
import type { Cast } from "../scene";
import { fixture } from "./fixture";

/** What a body may actually sit at or on. Everything else behind a seat is scenery. */
const PERCHES = new Set(["desk", "drafting", "sofa", "sofaG", "bench", "benchPark", "armchair", "chair"]);

interface Fault {
  room: string;
  seat: string;
  behind: string;
}

function main(): void {
  const cast = fixture().workers as Cast[];
  const world = worldFor(cast);
  const faults: Fault[] = [];
  let seats = 0;
  let perched = 0;

  for (const room of world.rooms) {
    const props = new Map(room.props.map((p) => [p.x + ":" + p.y, String(p.tile)]));
    const here: Fault[] = [];
    for (const seat of room.seats) {
      seats++;
      const key = (room.id || "(lobby)") + " " + (seat.post ?? "desk") + " (" + seat.x + "," + seat.y + ")";
      if (seat.perch === false) continue;
      perched++;
      const behind = props.get(seat.x + ":" + (seat.y - 1));
      if (!behind || !PERCHES.has(behind)) {
        here.push({ room: room.id || "(lobby)", seat: key, behind: behind ?? "nothing" });
      }
      // A place nobody can reach is a place nobody stands in. The cell itself must be walkable.
      if (world.solid[seat.y * world.cols + seat.x]) {
        here.push({ room: room.id || "(lobby)", seat: key, behind: "THE SEAT ITSELF IS SOLID" });
      }
    }
    faults.push(...here);
    const label = (room.id || "(lobby)").padEnd(12);
    console.log(`${label} seats=${String(room.seats.length).padStart(3)}  faults=${here.length}`);
  }

  /* ── AND NOBODY WALKS THROUGH A POTTED TREE ───────────────────────────────────────────────
     Six floating-tree reports narrowed to one class the prop rules could not reach: leafy decor
     beside a BODY. A body is two tiles tall and drawn over the map, so anybody ON the cell
     directly south of a plant swallows its pot and trunk and the canopy floats over their
     head — and a stop-only ban was measured insufficient: a body merely WALKING THROUGH that
     cell fused with the sprite at 94.8% for whole frames. So the pot claims its floor space as
     SOLID, exactly like the furniture's own footprint, and the router goes around. Checked from
     both sides — the pot-space cell must be solid (no route can cross it, which closes
     pass-through statically) and must carry no declared seat — because the set and the seats
     are maintained in different passes and either drifting alone re-ships the bug. */
  const LEAFY = new Set(["plant", "tree", "treeBig", "pine", "bush"]);
  const shySet = new Set(world.shy);
  const shaded: string[] = [];
  for (const room of world.rooms) {
    for (const seat of room.seats) {
      if (shySet.has(seat.y * world.cols + seat.x)) {
        shaded.push(`${room.id || "(lobby)"}: seat (${seat.x},${seat.y}) sits in a pot's floor space — a body there wears the canopy`);
      }
    }
    /* The yard is NOT skipped. It is an outdoor ROOM with real workers walking it, and its two
       trees sat outside the first solidity pass — ownSolid=0, southSolid=0, a web agent free to
       stand inside the pine. The exemption belongs to the grounds, where nobody can walk. */
    const seatsAt = new Set(room.seats.map((s) => s.x + ":" + s.y));
    for (const p of room.props) {
      if (room.outdoor && !world.solid[p.y * world.cols + p.x]) {
        shaded.push(`${room.id}: ${p.tile} at (${p.x},${p.y}) own cell is WALKABLE — the yard carve wiped its block`);
      }
      if (!LEAFY.has(String(p.tile))) continue;
      if (seatsAt.has(p.x + ":" + (p.y + 1))) {
        shaded.push(`${room.id || "(lobby)"}: ${p.tile} at (${p.x},${p.y}) has a declared seat directly SOUTH`);
      }
      const pot = (p.y + 1) * world.cols + p.x;
      if (!shySet.has(pot)) {
        shaded.push(`${room.id || "(lobby)"}: ${p.tile} at (${p.x},${p.y}) missing its pot-space cell`);
      }
      if (!world.solid[pot]) {
        shaded.push(`${room.id || "(lobby)"}: ${p.tile} at (${p.x},${p.y}) pot space is WALKABLE — a route can carry a body through the canopy`);
      }
    }
  }

  /* ── AND NO TWO PLACES MAY CROWD EACH OTHER ────────────────────────────────────────────────
     The second half of the rule, and the half that came back after being fixed once. A nameplate
     is capped at 66 world pixels against a 24-pixel tile, so two people two tiles apart wear
     plates that overlap by eighteen; and a body's capability glyphs hang at its feet and run
     twenty-two pixels past them, through the face of anybody two rows in front.
     A room emits rows of seats from FOUR different places — the desk banks, the standing
     overflow, the lobby's waiting bench, the yard's path — and a fix applied to one of them left
     the other three collided, which an independent critic found in two rooms in both themes. So
     the rule is checked over every PAIR, and the code has one constant for it. */
  const crowded: string[] = [];
  for (const room of world.rooms) {
    for (let i = 0; i < room.seats.length; i++) {
      for (let j = i + 1; j < room.seats.length; j++) {
        const a = room.seats[i];
        const b = room.seats[j];
        if (Math.abs(a.x - b.x) >= PITCH || Math.abs(a.y - b.y) >= PITCH) continue;
        crowded.push(
          `${room.id || "(lobby)"}: (${a.x},${a.y}) and (${b.x},${b.y}) are ` +
            `${Math.abs(a.x - b.x)}x${Math.abs(a.y - b.y)} apart, under the ${PITCH}-tile pitch`,
        );
      }
    }
  }

  /* ── AND THE PLACES A REAL CAST IS ACTUALLY GIVEN ─────────────────────────────────────────
     Everything above checks what the BUILDING declares. What a body stands on is what seating
     HANDS it, and those were two different sets: the hand-out used to wrap an exhausted pool by
     stepping one tile off a real place, inventing a coordinate the building never agreed to and
     that no invariant over room.seats could ever see. Three finished agents in a two-couch room
     put the third one a single tile from the first — sprites overlapping, nameplates illegible —
     while every check here reported the room correct. So the hand-out is checked too. */
  const given = [...seating(world, cast, brainOf(cast)).entries()];
  const byRoom = new Map<string, [string, { x: number; y: number }][]>();
  for (const [id, seat] of given) {
    const home = placeOf(world, cast.find((w) => w.run_id === id)!).id || "(lobby)";
    byRoom.set(home, [...(byRoom.get(home) ?? []), [id, seat]]);
  }
  const collided: string[] = [];
  for (const [room, list] of byRoom) {
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const [ai, a] = list[i];
        const [bi, b] = list[j];
        if (Math.abs(a.x - b.x) >= PITCH || Math.abs(a.y - b.y) >= PITCH) continue;
        collided.push(
          `${room}: ${ai} at (${a.x},${a.y}) and ${bi} at (${b.x},${b.y}) — ` +
            `${Math.abs(a.x - b.x)}x${Math.abs(a.y - b.y)} apart`,
        );
      }
    }
  }

  /* ── AND PAST CAPACITY, WHERE NOTHING RANDOM WILL EVER TAKE YOU ───────────────────────────
     A room is built with three places per two people, so no real roster can exhaust one — which
     is precisely why the overflow path shipped with a bug that stacked EVERY body past the first
     onto one identical tile, and why no fixture, no screenshot and no live sweep could have found
     it. Driven deliberately here: one room, three times more finished agents than it has places,
     and every place handed out must still be distinct and a PITCH apart. */
  {
    const room = world.rooms.find((r) => r.seats.length >= 4 && !!r.id && !r.open);
    if (room) {
      /* TWO CONTRACTS, split where the geometry splits them. Filled exactly to its declared
         capacity, a room hands out only declared places — distinct AND a pitch apart, because
         the building's own seats are. Flooded past capacity (three times over — a state no
         world sized from its own headcount can reach), the pitch is physically impossible:
         you cannot space fifty-four people three tiles apart on a twenty-by-twelve floor. What
         MUST survive any crowd is the hard floor of the whole mechanism — never two bodies on
         one coordinate, never a body outside its own room, never one standing in a wall. The
         old queue failed all three at once, BELOW the room, in the hall or the solid grounds. */
      const flood = (n: number): { x: number; y: number }[] => {
        const crowd: Cast[] = [];
        for (let i = 0; i < n; i++) {
          /* brain stripped: placeOf routes a brain-flagged body to the chamber whatever its
             department says, and cast[0] happens to be the fixture's brain — the whole flood
             quietly landed in the chamber while this probe reported on the room it never entered. */
          crowd.push({ ...cast[0], run_id: "flood" + i, status: "done", department: room.id, zone: "code", brain: false } as Cast);
        }
        return [...seating(world, crowd, null).values()];
      };
      const shares = (spots: { x: number; y: number }[]): number => {
        const seen = new Set<string>();
        let stacked = 0;
        for (const a of spots) {
          const key = a.x + ":" + a.y;
          if (seen.has(key)) stacked++;
          seen.add(key);
        }
        return stacked;
      };

      const exact = flood(room.seats.length);
      let tight = 0;
      for (let i = 0; i < exact.length; i++) {
        for (let j = i + 1; j < exact.length; j++) {
          if (Math.abs(exact[i].x - exact[j].x) < PITCH && Math.abs(exact[i].y - exact[j].y) < PITCH) tight++;
        }
      }
      const packed = flood(room.seats.length * 3);
      const inRoom = (p: { x: number; y: number }): boolean =>
        room.rects.some((r) => p.x >= r.x && p.x < r.x + r.w && p.y >= r.y && p.y < r.y + r.h);
      const escaped = packed.filter((p) => !inRoom(p)).length;
      const walled = packed.filter((p) => world.solid[p.y * world.cols + p.x]).length;
      console.log(
        `\noverflow: ${room.id} (${room.seats.length} places) at capacity -> ` +
          `${shares(exact)} stacked, ${tight} under the pitch; ` +
          `at 3x -> ${shares(packed)} stacked, ${escaped} outside the room, ${walled} in a wall.`,
      );
      if (shares(exact) || tight || shares(packed) || escaped || walled) {
        console.log("  OVERFLOW BREAKS ITS CONTRACT:", JSON.stringify(packed.map((a) => [a.x, a.y])));
        process.exitCode = 1;
      }
    }
  }

  /* ── AND A DEPARTMENT THAT FINISHES TOGETHER LIES DOWN TOGETHER ───────────────────────────
     The whole point of the two-ends room, measured at its worst case: the REAL registry has been
     one hundred percent finished, and at that state every body wants the rest end at once. A
     rest end sized to half the headcount pushed the losers of the seat race back onto the desks,
     standing — indistinguishable from ready, which is the one distinction the room exists to
     draw. So: the same cast with every status flipped to done must land EVERY body on a rest
     place with something to lie on directly behind it. Same world — headcounts ignore status —
     so this is the same building the mixed cast stands in. */
  {
    const allDone = cast.map((w) => ({ ...w, status: "done" }) as Cast);
    const spots = seating(world, allDone, brainOf(allDone));
    const standing: string[] = [];
    for (const w of allDone) {
      const s = spots.get(w.run_id);
      if (!s) continue;
      const room = placeOf(world, w);
      const behind = room.props.find((p) => p.x === s.x && p.y === s.y - 1);
      const couch = behind && ["sofa", "sofaG", "bench", "benchPark", "armchair"].includes(String(behind.tile));
      if (s.post !== "rest" || s.perch === false || !couch) {
        standing.push(
          `${room.id || "(lobby)"}: ${w.run_id} finished but got ${s.post ?? "desk"} at (${s.x},${s.y}), ` +
            `behind: ${behind?.tile ?? "nothing"} — a lounger with nothing to lie on stands, and reads as ready`,
        );
      }
    }
    console.log(`all-finished: ${allDone.length} done bodies, ${standing.length} without a couch.`);
    if (standing.length) {
      console.log("\nFINISHED BODIES LEFT STANDING:");
      for (const c of standing) console.log("  " + c);
      process.exitCode = 1;
    }
  }

  /* ── AND FOLIAGE STANDS IN SOIL, NEVER IN FURNITURE ───────────────────────────────────────
     Found on the REAL registry, never on a fixture: a plant sharing a cell with the chamber's
     whiteboard, and a plant one tile north of the front desk — the southern prop paints over the
     trunk and the canopy appears to grow out of the desk, which is what "trees are floating" was
     the fourth time it was reported. The builder now drops such a plant; this makes the rule an
     invariant so it cannot quietly return with the next furnisher change. */

  const planted: string[] = [];
  const propSets: [string, { x: number; y: number; tile: string }[]][] = [
    ...world.rooms.map((r): [string, { x: number; y: number; tile: string }[]] => [
      r.id || "(lobby)",
      r.props.map((p) => ({ x: p.x, y: p.y, tile: String(p.tile) })),
    ]),
    ["(hall)", world.hallProps.map((p) => ({ x: p.x, y: p.y, tile: String(p.tile) }))],
  ];
  for (const [where, props] of propSets) {
    const firm = new Set(props.filter((p) => !LEAFY.has(p.tile)).map((p) => p.x + ":" + p.y));
    for (const p of props) {
      if (!LEAFY.has(p.tile)) continue;
      if (firm.has(p.x + ":" + p.y)) planted.push(`${where}: ${p.tile}@(${p.x},${p.y}) shares a cell with furniture`);
      if (firm.has(p.x + ":" + (p.y + 1))) planted.push(`${where}: ${p.tile}@(${p.x},${p.y}) grows out of the prop south of it`);
    }
  }
  if (planted.length) {
    console.log("\nFOLIAGE PLANTED IN FURNITURE:");
    for (const c of planted) console.log("  " + c);
    process.exitCode = 1;
  }

  console.log(`\n${seats} places, ${perched} of them claiming something to sit on.`);
  console.log(`${given.length} people placed, ${collided.length} of them crowding somebody.`);
  console.log(`${deptsOf(cast).length} departments.`);
  if (collided.length) {
    console.log("\nPEOPLE HANDED PLACES TOO CLOSE TOGETHER:");
    for (const c of collided) console.log("  " + c);
  }
  if (crowded.length) {
    console.log("\nPLACES TOO CLOSE TOGETHER:");
    for (const c of crowded) console.log("  " + c);
  }
  if (shaded.length) {
    console.log("\nBODIES ALLOWED TO STAND UNDER A CANOPY:");
    for (const c of shaded) console.log("  " + c);
  }
  if (faults.length || crowded.length || collided.length || shaded.length) {
    if (faults.length) {
      console.log("\nPLACES WITH NOTHING BEHIND THEM:");
      for (const f of faults) console.log(`  ${f.seat} -> ${f.behind}`);
    }
    process.exit(1);
  }
  console.log(`Every place has the thing it belongs to behind it, and none is within ${PITCH} tiles of another.`);
}

main();
