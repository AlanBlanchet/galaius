/** The PLAN: a building with a spine, not a partition of a rectangle.
 *
 *  The version this replaces solved the floor by cutting one square into a 3x3 grid and giving
 *  each department a cell. Every room therefore came out the same size, the same shape and the
 *  same way up, corridors were whatever was left over, and the whole thing read — correctly — as
 *  a floor plan with pixel art painted on it. No amount of nicer tiles fixes that, because the
 *  defect is in the GENERATOR: a partition function can only ever produce a partition.
 *
 *  So the building is now composed the way a building is:
 *
 *   - a HALL runs through it, from the front gate to the chamber at its head. It has its own
 *     ground and a runner down the middle, so a passage looks nothing like the places it joins.
 *   - rooms BUD off the hall, north and south, sharing party walls. Each one is as WIDE as its
 *     headcount and as DEEP as its trade, so the band has a ragged back and the building has a
 *     silhouette. Where a shallow room leaves space behind it, that space is either the building's
 *     structural mass or an ALCOVE the room keeps for itself — which is where the L-shaped rooms
 *     come from, and where the one-of-a-kind things stand.
 *   - the wall a room FACES is two tiles: the cap you look down on and the face you look at.
 *     A one-tile band on all four sides is a border; a wall with a visible face is a wall, and it
 *     is the single cheapest thing that makes a room stop being a rectangle.
 *   - the CHAMBER at the head of the hall is not a room at all: no walls, a colonnade, a raised
 *     dais. Every route to the west half crosses it, which is the org chart drawn as architecture.
 *
 *  What a room CONTAINS comes from what its people can actually DO. A department whose members
 *  mostly run commands gets a machine room; one whose members only read gets the stacks. The
 *  faculties are read off each agent's own definition file, ranked by how much they distinguish
 *  that department from the rest of the company, and the top one picks the archetype. Nothing is
 *  keyed to a department NAME, so a company file with different departments furnishes itself.
 */
import type { TileId } from "./tiles";
import { footprint } from "./tiles";

/* ── the measure ─────────────────────────────────────────────────────────────────────────────
   Every number here is in TILES. A bay is one back-wall prop, one desk under it and two standing
   places three tiles apart — which is what a 36px character with a name over it actually needs. */

export const BAY = 3;
/** HOW FAR APART TWO STANDING PLACES MUST BE, in tiles, along BOTH axes.
 *
 *  Three, and it is not a taste number: a nameplate is capped at 66 world pixels and a tile is 24,
 *  so two people two tiles apart wear plates that overlap by eighteen; and a body's capability
 *  glyphs hang at its feet and run twenty-two pixels past them, straight through the face of
 *  anyone two rows in front. Both were measured, both were reported, and both came back — because
 *  the first fix was applied to ONE of the four places that emit a row of seats. It is a property
 *  of a PLACE, so it lives with the places, and `dev/placement.ts` fails the build if any pair in
 *  any room breaks it. */
export const PITCH = 3;
export const MIN_BAYS = 2;
/** The hall, and the runner down the middle of it. */
export const HALL_H = 3;
/** How far the outdoors reaches past the facade. */
const OUT = 7;
/** The chamber at the head of the building, before it is widened for a crowd. */
const CHAMBER_W = 9;
/** The lobby inside the front gate. */
const LOBBY_W = 8;
/** Grounds around the building, in tiles. The world has to be bigger than the level in BOTH axes
 *  or the camera runs out of somewhere to go — a tall narrow side bar letterboxes the plan
 *  otherwise — and bigger by ENOUGH: at four columns and no right margin there was nowhere to put
 *  a pond, a meadow or a tree line, so landscaping was silently clipped into the four-tile verge
 *  west of the building. */
const MX = 9;
const MY = 9;

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Dept {
  id: string;
  /** The room name as the company file words it — "Finance Desk", not "finance". */
  label: string;
  /** How many people are in it right now. A room bulges where the work is. */
  heads?: number;
  /** The faculties its people declare, most DISTINGUISHING first. The first one this file knows
   *  an archetype for decides what kind of room it is. Ranked by the caller, which is the only
   *  place that can see the whole company and therefore what counts as distinguishing. */
  kit?: readonly string[];
}

/** Which half of a room a standing place belongs to.
 *
 *  This is the whole placement model. A room used to be a bag of interchangeable standing places
 *  and a body was handed the next free one, so where somebody stood said nothing at all: an agent
 *  that had finished stood in the same spot, in the same pose, as one mid-command, and the only
 *  difference between them was a word stamped on a placard over their head. Two ranks, three rows
 *  apart, the back one facing nothing — "a person floating in the middle of a room with a desk two
 *  tiles away", which is exactly what it looked like.
 *
 *  Now a room has a WORKING end and a REST end, and which one you occupy is your state. You read
 *  a department's condition off where its people are before you read a single word.
 */
export type RoomEnd = "desk" | "rest";

export interface Seat {
  x: number;
  y: number;
  /** Wear your name ABOVE your head rather than under your feet: a rank standing three tiles in
   *  front of another otherwise has its head in the back rank's labels, and depth ordering draws
   *  the front body over them — the wrong person hiding the right person's name. */
  up?: boolean;
  /** Which end of the room. Handed out by state, not by arrival order. */
  post?: RoomEnd;
  /** Whether there is anything HERE to sit on.
   *
   *  Posture comes from a run's state, but a state cannot conjure furniture: a fetch agent
   *  standing in a field outside the front gate has no desk and no bench, and drawing it seated
   *  puts a person cross-legged on a lawn. So a place declares what it can offer and a seated
   *  posture falls back to standing where it cannot be honoured. Absent means yes. */
  perch?: boolean;
  /** Which way a body looks once it settles here. The sprite only mirrors, so this is worth
   *  nothing at a desk and everything on a couch: two people on one bench turned INWARD are
   *  sitting together, and the same two turned the same way are queuing. */
  face?: -1 | 1;
}

export interface Door {
  x: number;
  y: number;
  /** A door through a two-tile wall is two cells: the opening seen from above and the frame you
   *  walk through. Passing a body through both is visibly going THROUGH a wall. */
  deep: boolean;
}

/** Something drawn standing on a cell. `live` asks the renderer for an ambient overlay — a screen
 *  that flickers, a kettle that steams, a fan that turns — which is a separate node so the tile
 *  underneath is never redrawn to make it move. */
export type LiveId = "screen" | "steam" | "fan" | "lamp";
export interface Prop {
  x: number;
  y: number;
  tile: TileId;
  live?: LiveId;
}

export interface Room {
  /** The department id, `""` for the lobby, `__brain`, `__web`, or an open-floor marker. */
  id: string;
  label: string;
  /** Which archetype furnished it — the stylesheet tints a machine room colder than a library. */
  kind: string;
  /** The footprint, one or two rectangles. Two is an L: a room with an alcove behind it. */
  rects: Rect[];
  /** Bounding box of the footprint. */
  x: number;
  y: number;
  w: number;
  h: number;
  brain: boolean;
  outdoor: boolean;
  lobby: boolean;
  /** No walls and no door: a part of the floor rather than a room. */
  open: boolean;
  doors: Door[];
  props: Prop[];
  seats: Seat[];
  floor: TileId;
  /** Horizontal runs, worked out here so the renderer emits rectangles and never walks cells. */
  floorRuns: Rect[];
  wallRuns: Rect[];
  faceRuns: Rect[];
  /** A raised platform under the middle of the room, drawn as ground rather than as furniture. */
  dais?: Rect;
  /** A change of ground in the middle of the floor. Blocks nothing. */
  rug?: Rect;
}

export interface World {
  cols: number;
  rows: number;
  rooms: Room[];
  byId: Map<string, Room>;
  /** cols*rows, true where a body may NOT stand. Doors are always false. */
  solid: boolean[];
  facadeX: number;
  gateY: number;
  brainRoom: Room;
  lobby: Room;
  yard: Room;
  /** The spine, for the renderer: its ground, and the runner down the middle of it. */
  hall: Rect;
  runner: Rect;
  /** The building's structural mass — everything inside the envelope that is neither room nor
   *  passage. Drawn as solid wall, which is what gives the plan weight. */
  mass: Rect[];
  /** The building itself, inside the grounds. */
  envelope: Rect;
  /** Things standing in the passage, and things standing outside the building. */
  hallProps: Prop[];
  scenery: Prop[];
  /** The GROUND of the site, where it is not plain lawn — meadow left unmown, bare earth, water.
   *  Emitted as horizontal runs per kind, exactly like a room's floor, because a patch of ground
   *  is one rectangle of a pattern and never five hundred tile elements. */
  terrain: { tile: TileId; runs: Rect[] }[];
  /** The pot's own floor space: the cell south of every indoor leafy prop, SOLID like the
   *  furniture's footprint, so no route ever carries a body through a potted tree. Exposed for
   *  the placement probe. */
  shy: number[];
  /** The street out of the gate: its top row, so the renderer can run it past the world's own
   *  columns into the drawn grounds — a road that stops at the data's edge reads as a driveway. */
  street: { y: number };
}

function hash32(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/* ── what a department DOES ──────────────────────────────────────────────────────────────────
   One archetype per faculty. The lists are four long and indexed by bay PLUS the room's own hash,
   so two bays in a row are different things and two rooms of the same trade are furnished in a
   different order — the "plant, monitor, plant" that repeated identically three times per row was
   a single list read from zero in every room. */

interface Archetype {
  kind: string;
  /** Interior rows.
   *
   *  EIGHT is the floor now, not six, and the reason is a bug the old number hid. The interior
   *  rows are `depth - 3`, the front rank stood at `iy + 2` and the back rank at `iy + depth-4` —
   *  which for a depth-6 room is the SAME ROW. Both ranks landed on identical cells, the seat
   *  de-duplicator walked one of them to "the nearest free cell", and that arbitrary cell is
   *  where a third of the company was standing. It has no visible symptom: everyone is on the
   *  floor, nobody is in a wall, and the placement is simply meaningless.
   *
   *  A room now needs: a back-wall row, a bank of desks and its rank, a SECOND bank and its rank,
   *  open floor, the rest furniture and the rest rank. Two banks, because a bay is three tiles
   *  wide and one bay is one desk — so with the old single rank a six-person department had three
   *  places for six people and the overflow was pushed one row NORTH, which is the desk row. That
   *  is what the close-up showed: two ranks stacked on top of the furniture they were supposed to
   *  be working at. A room holds its people or it is the wrong size. */
  depth: number;
  /** Against the back wall, one per bay. */
  back: TileId[];
  /** On the wall face above it. */
  face: TileId[];
  /** The working surface in front of the back wall. */
  desk: TileId;
  floor: TileId;
  /** The one thing this trade has that no other does. Placed once, in the alcove where there is
   *  one and beside the door where there is not. */
  feature: TileId;
}

const ARCHETYPES: Record<string, Archetype> = {
  reads: {
    kind: "stacks",
    depth: 12,
    back: ["stacks", "stacks", "ladder", "cabinet"],
    face: ["pinboard", "winFace", "poster", "clock"],
    desk: "desk",
    floor: "carpet",
    feature: "globe",
  },
  writes: {
    kind: "drafting",
    depth: 11,
    back: ["cabinet", "shelf", "crates", "cabinet"],
    face: ["winFace", "pinboard", "poster", "winFace"],
    desk: "drafting",
    floor: "carpet",
    feature: "printer",
  },
  runs: {
    kind: "machine",
    depth: 11,
    back: ["rack", "rack", "crates", "rack"],
    face: ["pipes", "vent", "screenWall", "pipes"],
    desk: "desk",
    floor: "floor",
    feature: "printer",
  },
  sees: {
    kind: "gallery",
    depth: 11,
    back: ["screen", "board", "screen", "shelf"],
    face: ["screenWall", "clock", "screenWall", "winFace"],
    desk: "desk",
    floor: "carpet",
    feature: "tank",
  },
  searches: {
    kind: "signals",
    depth: 12,
    back: ["dish", "globe", "board", "rack"],
    face: ["winFace", "poster", "pinboard", "winFace"],
    desk: "desk",
    floor: "floor",
    feature: "dish",
  },
  delegates: {
    kind: "boardroom",
    depth: 11,
    back: ["board", "cabinet", "board", "shelf"],
    face: ["whiteboard", "clock", "whiteboard", "poster"],
    desk: "desk",
    floor: "carpet",
    feature: "tableM",
  },
};

const COMMONS: Archetype = {
  kind: "commons",
  depth: 11,
  back: ["sofa", "plant", "urn", "sofa"],
  face: ["poster", "winFace", "clock", "pinboard"],
  desk: "bench",
  floor: "carpet",
  feature: "coffee",
};

/** EVERYTHING THAT HANGS ON A WALL, and therefore stands on nothing.
 *
 *  A poster, a window, a clock, a pinboard and a wall lamp are placed on the FACE row — the
 *  masonry, not the floor — and every one of them is drawn as a full eight-by-eight solid tile.
 *  Cast like a standing thing, each threw a full-width slab of shade onto the floor in front of
 *  it, on top of the band `wallShadow` already lays there for the wall itself. Twice-darkened
 *  masonry with a hard edge a tile out from the wall is not a shadow of anything; it is the same
 *  "a shape of ink where the ground is" defect as a floating tree, arriving from the other side.
 *  A wall lamp is worse than wrong — it is the light SOURCE, throwing its own shadow.
 *
 *  So they are DELIBERATELY FLAT, and this is where that decision is written down rather than
 *  being an omission somebody has to infer. In a north-west light a poster's own shade falls on
 *  the wall it is screwed to, a lip perhaps one cell wide; at three device pixels a cell that is
 *  not a shadow, it is a smudge.
 *
 *  DERIVED from the archetypes rather than hand-listed, because a hand-kept copy of a list is how
 *  four earlier bugs on this view happened: add a fixture to any room's `face` and it is flat the
 *  same minute, with nobody having to remember this set exists. */
export const WALL_FIXTURES: ReadonlySet<TileId> = new Set<TileId>([
  ...Object.values(ARCHETYPES).flatMap((a) => a.face),
  ...COMMONS.face,
  // Always hung, never listed in an archetype: every room gets one lamp on its face.
  "lamp",
]);

/** The plants and screens the stylesheet is asked to animate. Kept here so a prop cannot acquire
 *  ambient motion by accident: a tile is alive because the plan said so. */
const LIVE_OF: Partial<Record<TileId, LiveId>> = {
  /* No plant here, deliberately: pot plants swayed for two weeks and the owner read it as
     "plants moving inside" — the eighth "floating trees" report, finally decoded. A plant has
     no STATE to express, so it earns no ambient motion; equipment is live because on/off means
     something. Motion carries meaning or it is noise. */
  screen: "screen",
  rack: "screen",
  board: "screen",
  screenWall: "screen",
  coffee: "steam",
  desk: "screen",
  lamp: "lamp",
  tank: "screen",
};

/** Things there is only ONE of in a building. A room asks for one and gets it if nobody already
 *  took it — which is the whole mechanism against "decorative filler repeats identically three
 *  times per row": the second room that wants a coffee machine does not get one. */
const ONCE: TileId[] = ["coffee", "tank", "vending", "cooler", "piano", "globe", "ladder", "printer", "dish", "stove", "mirror"];

class Uniques {
  private taken = new Set<string>();
  claim(tile: TileId): boolean {
    if (!ONCE.includes(tile)) return true;
    if (this.taken.has(tile)) return false;
    this.taken.add(tile);
    return true;
  }
  /** The best still-unclaimed thing from a wish list, in order. */
  pick(wish: readonly TileId[]): TileId | null {
    for (const t of wish) if (this.claim(t)) return t;
    return null;
  }
}

/* ── from rectangles to walls ────────────────────────────────────────────────────────────────
   The footprint is one or two rectangles; everything else is derived. A cell inside the footprint
   whose eight neighbours are all inside is floor; anything else inside is wall. Then every wall
   run with floor directly BELOW it is thickened by one and that second row is its FACE — which is
   what turns a plan into a room you are looking into. L-shapes fall out for free: the derivation
   never knew the footprint was a rectangle in the first place. */

interface Shape {
  wall: Set<number>;
  face: Set<number>;
  floor: Set<number>;
  box: Rect;
}

function shapeOf(rects: readonly Rect[], stride: number): Shape {
  const x0 = Math.min(...rects.map((r) => r.x));
  const y0 = Math.min(...rects.map((r) => r.y));
  const x1 = Math.max(...rects.map((r) => r.x + r.w));
  const y1 = Math.max(...rects.map((r) => r.y + r.h));
  const inside = (x: number, y: number): boolean =>
    rects.some((r) => x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h);
  const core = (x: number, y: number): boolean => {
    if (!inside(x, y)) return false;
    for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) if (!inside(x + dx, y + dy)) return false;
    return true;
  };
  const wall = new Set<number>();
  const face = new Set<number>();
  const floor = new Set<number>();
  for (let y = y0; y < y1; y++) {
    for (let x = x0; x < x1; x++) {
      if (!inside(x, y)) continue;
      (core(x, y) ? floor : wall).add(y * stride + x);
    }
  }
  // The second row of every wall that has floor under it. Done after the first pass so the pass
  // itself is not reading a set it is still writing.
  for (const key of [...wall]) {
    const x = key % stride;
    const y = (key - x) / stride;
    const below = (y + 1) * stride + x;
    if (!floor.has(below)) continue;
    floor.delete(below);
    wall.add(below);
    face.add(below);
  }
  return { wall, face, floor, box: { x: x0, y: y0, w: x1 - x0, h: y1 - y0 } };
}

/** Horizontal runs of a cell set, as rectangles. One patch per run instead of one node per cell:
 *  a forty-by-thirty building is about two hundred elements rather than twelve hundred. */
function runsOf(cells: Set<number>, box: Rect, stride: number): Rect[] {
  const out: Rect[] = [];
  for (let y = box.y; y < box.y + box.h; y++) {
    let x = box.x;
    while (x < box.x + box.w) {
      if (!cells.has(y * stride + x)) {
        x++;
        continue;
      }
      let run = 1;
      while (cells.has(y * stride + x + run)) run++;
      out.push({ x, y, w: run, h: 1 });
      x += run;
    }
  }
  return out;
}

/* ── furnishing ──────────────────────────────────────────────────────────────────────────────
   The skeleton is the same in every trade — things against the back wall, a working surface in
   front of them, two ranks of standing room — and everything ON that skeleton comes from the
   archetype and the room's own hash. That is deliberate: a place is legible because its shape is
   familiar and its contents are not. */

function furnish(
  room: Room,
  arch: Archetype,
  bays: number,
  uniques: Uniques,
  seed: number,
  restBanks = 1,
): void {
  const main = room.rects[0];
  /* THE DOOR APPROACH IS SACRED, and every emitter checks it — not only the clutter pass.
     Two rooms sealed themselves shut on a real roster and neither was clutter: the PLANT laid
     between two couches claimed the cell south of itself as solid (its pot's floor space), and
     the one-of-a-kind feature walking north off the couch row stopped exactly over the door's
     inside approach. Both emitters ran before the old `forbidden` set even existed. A prop that
     seals a department is worse than no prop at any position, so the door's whole column — and
     the cell a leafy piece would claim beneath itself — is checked by everything placed here. */
  const doorSafe = new Set<string>();
  for (const d of room.doors) {
    for (let k = -4; k <= 4; k++) {
      doorSafe.add(d.x + ":" + (d.y + k));
      doorSafe.add(d.x - 1 + ":" + (d.y + k));
      doorSafe.add(d.x + 1 + ":" + (d.y + k));
    }
  }
  const sealing = (x: number, y: number, tile: TileId): boolean =>
    doorSafe.has(x + ":" + y) || (tile === "plant" && doorSafe.has(x + ":" + (y + 1)));
  // A recess deep enough to stand something in. The plan forms also emit a WING — two or three
  // rows, most of it wall — and treating that as an alcove put the room's one-of-a-kind prop
  // inside the masonry, outside every room, on the building's own mass.
  const wing = room.rects[1] ?? null;
  const alcove = wing && wing.h >= 4 ? wing : null;
  const ix = main.x + 1; /* first interior column */
  const iy = main.y + 2; /* first interior row, under the wall face */
  const D = main.h - 3; /* interior rows */
  const put = (x: number, y: number, tile: TileId): void => {
    room.props.push({ x, y, tile, live: LIVE_OF[tile] });
  };

  /* TWO BANKS OF DESKS. A bay is three tiles and one bay is one desk, so a single rank gave a
     six-person department three places — and the overflow was pushed a row NORTH onto the desks
     themselves. The second bank stands in the open, with no back wall behind it, which is what an
     open-plan floor actually looks like and what makes the room read as deeper than one row of
     furniture glued to a wall. */
  const banks = 2;
  for (let i = 0; i < bays; i++) {
    const cx = ix + i * BAY + 1;
    put(cx, iy, arch.back[(i + seed) % arch.back.length]);
    for (let bank = 0; bank < banks; bank++) {
      // Every OTHER desk gets a lit screen. All of them would be forty animations in one room and
      // a wall of blinking; every other one reads as a floor where some people are at a machine.
      room.props.push({
        x: cx,
        y: iy + 1 + bank * PITCH,
        tile: arch.desk,
        live: (i + seed + bank) % 2 === 0 ? LIVE_OF[arch.desk] : undefined,
      });
    }
  }

  /* ── the two ends of the room ────────────────────────────────────────────────────────────
     THE WORKING END is the desk rank: one place per bay, directly in front of its own desk, so a
     body settled there is at a specific machine rather than somewhere in the room's general
     direction. THE REST END is the far row: a couch, a bench and a low table against the back of
     the room, with places in front of them and each one turned INWARD.

     The rank that used to sit here faced nothing and belonged to nothing — and in a depth-6 room
     it was arithmetically the same row as the desk rank, so both collapsed onto one another and
     the de-duplicator scattered the overflow. */
  const restRow = iy + D - 1;
  /* The SHALLOWEST rest row: everything between the desks and the rest end measures itself
     against this, not against the deep row — a standing place gated against the far couch bank
     lands two tiles from the near one, which is the pitch violation wearing a different bank. */
  const restRowNear = restRow - (restBanks - 1) * PITCH;
  const restProps = restRowNear - 1;
  for (let bank = 0; bank < banks; bank++) {
    for (let i = 0; i < bays; i++) {
      room.seats.push({
        x: ix + i * BAY + 1,
        y: iy + 2 + bank * PITCH,
        // The front bank wears its name over its head: the bank behind it is three tiles back and
        // would otherwise have its own head in the front rank's labels.
        up: bank === 0,
        post: "desk",
        face: i % 2 ? -1 : 1,
      });
    }
  }
  /* The furniture first, then the places in front of it, so a body that sits down has something
     behind it rather than sitting on the floor. Alternating pieces: a two-cell couch reads as a
     couch only when what is beside it is NOT another couch. Everything in the kit is something a
     person can LIE on — the finished lounge horizontally, and a lounger across an armchair is a
     body clipping through furniture.

     BANKS OF COUCHES, exactly like the banks of desks, and for the same reason: one row was
     `bays` places for up to `2*bays` people, so the moment a department finished together half
     of it lost the seat race and stood at the desks — indistinguishable from ready, which is the
     one distinction the two-ends room exists to draw. The deep bank fills first, against the
     back wall. */
  const restKit: TileId[] = ["sofa", "bench", "sofa", "sofa"];
  for (let bank = 0; bank < restBanks; bank++) {
    const seatY = restRow - bank * PITCH;
    for (let i = 0; i < bays; i++) {
      const cx = ix + i * BAY + 1;
      put(cx, seatY - 1, restKit[(i + bank + seed) % restKit.length]);
      // A low table between two seats, and greenery at the ends: the difference between a row of
      // chairs and a place somebody would actually sit. Never in the door's own column.
      if (i > 0) {
        const between: TileId = (i + bank + seed) % 2 === 0 ? "tableM" : "plant";
        if (!sealing(cx - 1, seatY - 1, between)) put(cx - 1, seatY - 1, between);
      }
      room.seats.push({
        x: cx,
        y: seatY,
        post: "rest",
        // Turned inward, in pairs. Two people on one bench looking the same way are a queue.
        face: i % 2 === 0 ? 1 : -1,
      });
    }
  }

  // The face: a fixture over every second bay, offset by the room's hash so no two rooms carry
  // the same things in the same order.
  const faceY = main.y + 1;
  for (let i = 0; i < bays; i++) {
    if ((i + seed) % 2 === 1 && bays > 2) continue;
    room.props.push({
      x: ix + i * BAY + 1,
      y: faceY,
      tile: arch.face[(i + seed + 1) % arch.face.length],
      live: LIVE_OF[arch.face[(i + seed + 1) % arch.face.length]],
    });
  }
  // One wall lamp, always on the face, always off-centre.
  room.props.push({ x: ix + ((seed % Math.max(1, bays)) * BAY), y: faceY, tile: "lamp", live: "lamp" });

  /* And places to simply STAND, in the open floor between the banks and the rest end. They are
     the last resort: a room with four times as many places as bays can never run out and push a
     body onto its own furniture, which is the failure this whole section exists to end. Marked
     with nothing to sit on, so anybody sent here is drawn standing. */
  {
    const y = iy + 2 + banks * PITCH;
    // Clear of the NEAREST rest row by the SAME pitch it is clear of the desks by. It was only
    // ever checked against the furniture above it, so in a room of this depth it landed two rows
    // from the couches and a standing agent's nameplate cut across a seated one's head.
    if (y + PITCH <= restRowNear) {
      for (let i = 0; i < bays; i++) {
        room.seats.push({ x: ix + i * BAY + 1, y, post: "rest", face: i % 2 ? -1 : 1, perch: false });
      }
    }
  }

  // The floor between the desks and the door. Left bare it is the "vast empty carpet" that made
  // every room read as a rectangle with props glued to one edge; it is where the things a room
  // accumulates go. Never on a standing place, and the list is walked from the room's own hash so
  // no two rooms accumulate the same things in the same order.
  /* No leafy decor in the mid-floor clutter — clutter lands on exactly the open cells bodies
     roam, and a stroller pausing south of a plant wears its canopy (the floating-tree class). */
  const clutter: TileId[] = ["chair", "crates", "bench", "cabinet", "sofa", "urn", "chair", "printer"];
  // What a chair may NEVER stand on. Every standing place AND its four neighbours, so a body
  // always has somewhere to step and somebody can always come and stand beside it; and the two
  // cells inside every door, because a crate there seals the department. Both were found by the
  // building's own invariants — a seat inside the masonry and a room nothing could path into —
  // never by looking at it.
  const forbidden = new Set<string>();
  for (const p of room.seats) {
    forbidden.add(p.x + ":" + p.y);
    forbidden.add(p.x + 1 + ":" + p.y);
    forbidden.add(p.x - 1 + ":" + p.y);
    forbidden.add(p.x + ":" + (p.y + 1));
    forbidden.add(p.x + ":" + (p.y - 1));
  }
  for (const d of room.doors) {
    for (let k = -3; k <= 3; k++) forbidden.add(d.x + ":" + (d.y + k));
    for (let k = -1; k <= 1; k++) forbidden.add(d.x + k + ":" + (d.y + (d.deep ? 2 : -2)));
  }
  let c = seed;
  for (let row = iy + 2 + banks * PITCH + 1; row < restProps; row++) {
    for (let i = 0; i < bays; i++) {
      c++;
      if ((c * 7 + row * 3) % 4) continue;
      const cx = ix + i * BAY + (c % 3);
      if (forbidden.has(cx + ":" + row) || sealing(cx, row, clutter[c % clutter.length])) continue;
      put(cx, row, clutter[c % clutter.length]);
    }
  }
  // And a rug under the middle of it, which is ground rather than furniture: a room whose whole
  // interior is one colour is waiting space, not a room.
  if (main.w >= 8 && D >= 11) {
    room.rug = { x: ix + 1, y: iy + 2 + banks * PITCH + 1, w: main.w - 4, h: Math.min(2, D - 11) };
  }

  // The one-of-a-kind thing. It stands in the alcove where the room has one — which is exactly
  // what an alcove is for — and otherwise just inside the door.
  const wish: TileId[] = [arch.feature, "cooler", "vending", "crates", "cabinet"];
  const only = uniques.pick(wish) ?? "crates";
  if (alcove) {
    const ax = alcove.x + Math.floor(alcove.w / 2);
    const ay = alcove.y + 2;
    put(ax, ay, only);
    // "boxes" was in this list, cast to TileId, and there is no such tile. The cast is what let it
    // through the type system, and the room shapes this list is walked for never happened to reach
    // it until the plan forms changed — so the whole map threw on an undefined grid. A tile id that
    // needs a cast to compile is an id that does not exist.
    const mate = uniques.pick(["stove", "coffee", "printer", "cabinet", "crates"]) ?? "crates";
    put(ax + 1, ay + 1, mate);
  } else {
    /* The fixed row collided with the NEAR couch bank the moment the rest end grew a second
       bank: a tank standing flush between a bench and a plant fused three contact shadows into
       one 23-cell bar — the slab rule's limit is sixteen. So the one-of-a-kind thing walks NORTH
       off its spot until it stands with no caster beside it on its own row and no seat within a
       tile. For a one-bank room the first candidate is already clear and nothing moves. */
    const fx = Math.min(ix + (bays - 1) * BAY + 2, main.x + main.w - 2);
    let fy = iy + 2 + banks * PITCH;
    for (let y = fy; y >= iy + 2; y--) {
      const beside = room.props.some((p) => p.y === y && Math.abs(p.x - fx) <= 1);
      const seated = room.seats.some((s) => Math.abs(s.x - fx) <= 1 && Math.abs(s.y - y) <= 1);
      if (!beside && !seated && !sealing(fx, y, only)) { fy = y; break; }
    }
    if (!sealing(fx, fy, only)) put(fx, fy, only);
  }
}

/** An open piece of floor: no walls, no door, and furniture that is there to be sat on rather
 *  than worked at. Every part of the building the departments do not fill becomes one, so a small
 *  company gets a building with a plaza in it rather than a blank quadrant. */
function loosen(room: Room, uniques: Uniques, seed: number): void {
  const b = room.rects[0];
  const put = (x: number, y: number, tile: TileId): void => {
    room.props.push({ x, y, tile, live: LIVE_OF[tile] });
  };
  /* No plants in a plaza's own kit — the whole plaza is open floor bodies roam, which is the
     floating-canopy class. The greenery an open floor gets is the grounds outside its windows. */
  const wish: TileId[] = ["piano", "coffee", "cooler", "tank", "vending"];
  const kit: TileId[] = ["sofa", "urn", "bench", "tableM", "crates"];
  let n = 0;
  for (let y = b.y + 1; y < b.y + b.h - 1; y += 3) {
    for (let x = b.x + 2; x < b.x + b.w - 2; x += 4) {
      n++;
      if (n === 2) {
        const only = uniques.pick(wish);
        if (only) {
          put(x, y, only);
          continue;
        }
      }
      put(x, y, kit[(n + seed) % kit.length]);
    }
  }
  /* A plaza is where you stand around, so every place in it is a REST place — and the ones that
     are actually in front of a couch are the ones you may sit on. Laid out FROM the furniture
     rather than on a lattice of their own: a seat three cells from the nearest bench is the
     "person floating in the middle of the room" defect, one room type further out. */
  const mid = b.x + b.w / 2;
  const sittable = new Set<string>(["sofa", "bench"]);
  for (const prop of room.props) {
    if (!sittable.has(prop.tile)) continue;
    room.seats.push({
      x: prop.x,
      y: prop.y + 1,
      up: true,
      post: "rest",
      face: prop.x < mid ? 1 : -1,
    });
  }
  // And somewhere to simply stand, for the overflow and for anybody the company filed under no
  // department at all — never within the pitch of a couch's seat laid above.
  for (let y = b.y + 3; y < b.y + b.h - 2; y += PITCH + 1) {
    for (let x = b.x + 4; x < b.x + b.w - 2; x += PITCH + 1) {
      if (room.seats.some((s) => Math.abs(s.x - x) < PITCH && Math.abs(s.y - y) < PITCH)) continue;
      room.seats.push({ x, y, post: "rest", face: x < mid ? 1 : -1, perch: false });
    }
  }
}

/* ── the building ────────────────────────────────────────────────────────────────────────────*/

const baysOf = (heads: number): number => Math.max(MIN_BAYS, Math.ceil(heads / 2));
/** The archetype for one department, given the ones already handed out. A department takes the
 *  trade its people most distinguish themselves by; if the room next door already IS that, it
 *  takes the next one down its own list. Two identical rooms is the repetition defect one level
 *  up from the furniture, and it is the level a reader notices first. */
const archOf = (kit: readonly string[] | undefined, taken: Set<string>): Archetype => {
  for (const f of kit ?? []) {
    const a = ARCHETYPES[f];
    if (a && !taken.has(a.kind)) return a;
  }
  for (const f of kit ?? []) if (ARCHETYPES[f]) return ARCHETYPES[f];
  return COMMONS;
};

interface Slot {
  dept: Dept;
  arch: Archetype;
  bays: number;
  /** How many banks of couches the rest end carries. A room must hold its WHOLE headcount at
   *  EITHER end — every desk taken is a floor at work, every couch taken is a floor that has
   *  finished, and both are real states of a real registry. Desks already scale (two banks of
   *  one-per-bay); the rest end scales the same way, and the room gets a pitch deeper for it. */
  restBanks: number;
  w: number;
  h: number;
}

function slotFor(dept: Dept, taken: Set<string>): Slot {
  const arch = archOf(dept.kit, taken);
  taken.add(arch.kind);
  const heads = dept.heads ?? 0;
  const bays = baysOf(heads);
  const restBanks = heads > bays ? 2 : 1;
  return { dept, arch, bays, restBanks, w: BAY * bays + 2, h: arch.depth + 3 + (restBanks - 1) * PITCH };
}

function blank(id: string, label: string, rects: Rect[], over: Partial<Room> = {}): Room {
  const x = Math.min(...rects.map((r) => r.x));
  const y = Math.min(...rects.map((r) => r.y));
  const w = Math.max(...rects.map((r) => r.x + r.w)) - x;
  const h = Math.max(...rects.map((r) => r.y + r.h)) - y;
  return {
    id,
    label,
    kind: "room",
    rects,
    x,
    y,
    w,
    h,
    brain: false,
    outdoor: false,
    lobby: false,
    open: false,
    doors: [],
    props: [],
    seats: [],
    floor: "carpet",
    floorRuns: [],
    wallRuns: [],
    faceRuns: [],
    ...over,
  };
}

/** The whole building, solved from a department list. Pure and deterministic: the same
 *  departments give byte-identical output, which is what lets the map be rendered once and never
 *  re-sent when somebody walks. */
export function buildWorld(
  depts: readonly Dept[],
  core: { brain?: number; lobby?: number; web?: number } = {},
): World {
  const uniques = new Uniques();

  // Alternate the departments between the two bands in declaration order, so the first ones
  // declared face each other across the hall rather than filling one side first.
  const kinds = new Set<string>();
  const slots = depts.map((d) => slotFor(d, kinds));
  const north: Slot[] = [];
  const south: Slot[] = [];
  slots.forEach((s, i) => (i % 2 === 0 ? north : south).push(s));

  const bandW = (band: Slot[]): number => band.reduce((n, s) => n + s.w - 1, 1);
  const bandH = (band: Slot[]): number => Math.max(9, ...band.map((s) => s.h));

  const chamberW = Math.max(CHAMBER_W, BAY * baysOf(core.brain ?? 0) + 4);
  const NB = bandH(north);
  const SB = bandH(south);
  const wallY = MY; /* the building's own top wall, with grounds above it */
  const hallY = wallY + 1 + NB;
  const floorY = hallY + HALL_H + SB; /* its bottom wall */
  const rows = floorY + 1 + MY;
  const bandStart = MX + 1 + chamberW;
  const bandSpan = Math.max(14, bandW(north), bandW(south));
  const lobbyX = bandStart + bandSpan;
  const facadeX = lobbyX + LOBBY_W;
  const cols = facadeX + 1 + OUT + MX;
  const gateY = hallY + 1;

  const rooms: Room[] = [];

  /** THE PLAN FORM of one room, and the reason the building has a silhouette.
   *
   *  Every room used to be a rectangle. Sized differently, furnished differently, lit differently
   *  — and still eight rectangles in two rows, which is what a floor PLAN looks like and not what
   *  a place looks like. A rectangle has nothing to stand behind, no corner to turn, and no wall
   *  that runs out and comes back, so nothing in it can ever be tucked away.
   *
   *  Three forms, chosen by the department's own hash so a company file draws the same building
   *  every time. All three keep the HALL-FACING edge at full width, because that edge carries the
   *  door and the sign, and a door on a stepped wall is a door nobody can find.
   *
   *   - BAR      a plain rectangle. Small rooms stay rectangles; an L in four tiles is a corridor.
   *   - ELL/TEE  the room is set back from the outer wall and one WING pushes out to it. At an end
   *              that is an L; in the middle it is a T. The step is what the eye reads.
   *   - ALCOVE   where there is spare depth behind the room, it keeps it as a recess — which is
   *              where `furnish` already puts the one-of-a-kind thing in each trade.
   */
  const plan = (x: number, y: number, w: number, h: number, side: "n" | "s", spare: number, seed: number): Rect[] => {
    const north = side === "n";
    // Deep enough behind it to keep a recess: that reads better than a step, so it wins.
    if (spare >= 3 && w >= 8) {
      const aw = Math.min(6, w - 3);
      const ax = x + 1 + (seed % Math.max(1, w - aw - 1));
      return [
        { x, y, w, h },
        north ? { x: ax, y: y - spare, w: aw, h: spare + 2 } : { x: ax, y: y + h - 2, w: aw, h: spare + 2 },
      ];
    }
    // Too small to step without becoming a corridor. Measured against the rooms this actually
    // produces: at a floor of six tiles a three-tile wing still leaves three, which reads as a
    // step; the first guard I wrote asked for nine and left four rooms out of five rectangular.
    if (w < 7 || h < 5) return [{ x, y, w, h }];
    const ww = 3 + ((seed >>> 3) % Math.max(1, w - 6));
    // End or middle. Two thirds of rooms take an end, so the band reads as steps rather than as a
    // row of identical castles.
    const where = (seed >>> 11) % 3;
    const wx = where === 0 ? x : where === 1 ? x + w - ww : x + Math.floor((w - ww) / 2);
    // The wing pushes OUT into whatever depth is left rather than being carved out of the room:
    // the first version set the whole room back two rows and gave one strip of it back, which
    // stepped the silhouette correctly and cost every department a fifth of its floor. Where
    // there is genuinely nothing behind, the room gives up ONE row for the step.
    const out = Math.min(2, spare);
    const back = out > 0 ? 0 : 1;
    const deep = out > 0 ? out + 1 : 2;
    return north
      ? [
          { x, y: y + back, w, h: h - back },
          { x: wx, y: y + back - deep + 1, w: ww, h: deep },
        ]
      : [
          { x, y, w, h: h - back },
          { x: wx, y: y + h - back - 1, w: ww, h: deep },
        ];
  };

  /** One band of rooms, budding off the hall. They share party walls: the next room starts on the
   *  previous one's wall, which is what stops a row of rooms reading as a row of boxes. */
  const layBand = (band: Slot[], side: "n" | "s"): void => {
    const avail = side === "n" ? NB : SB;
    let x = bandStart;
    for (const s of band) {
      const y = side === "n" ? hallY - s.h : hallY + HALL_H;
      const seed = hash32(s.dept.id) >>> 0;
      const rects: Rect[] = plan(x, y, s.w, s.h, side, avail - s.h, seed);
      const room = blank(s.dept.id, s.dept.label, rects, {
        kind: s.arch.kind,
        floor: s.arch.floor,
      });
      // The door is on the wall that faces the hall: shallow on a north room (you look over its
      // low south wall), two cells deep on a south room (you walk through the face).
      // UNSIGNED shift. The hash is a uint32, so a signed >> turns it negative above 2^31, the
      // modulus comes out negative, and the door is placed to the LEFT of its own room — which is
      // what happened: one department's door landed three tiles inside its neighbour and nothing
      // could path into it. Invisible on screen (it looked like a door), caught by the invariant
      // that every room must be reachable from every other.
      const doorX = x + 1 + ((seed >>> 5) % Math.max(1, s.w - 3));
      room.doors.push(
        side === "n" ? { x: doorX, y: y + s.h - 1, deep: false } : { x: doorX, y, deep: true },
      );
      furnish(room, s.arch, s.bays, uniques, seed % 4, s.restBanks);
      rooms.push(room);
      x += s.w - 1;
    }
    // Whatever the shorter band does not use is open floor, not corridor: a plaza with seats in
    // it, which is also where anybody the company filed under no department can stand.
    const rest = bandStart + bandSpan - x;
    if (rest >= 6) {
      const y = side === "n" ? wallY + 1 : hallY + HALL_H;
      const room = blank("__open" + side, "", [{ x, y, w: rest, h: avail }], {
        open: true,
        kind: "plaza",
        floor: "floor",
      });
      loosen(room, uniques, side === "n" ? 1 : 3);
      rooms.push(room);
    }
  };

  // The chamber at the head of the hall. Not a room: no walls, a colonnade and a raised dais, so
  // the one place everybody's routes cross is the one place that is not four walls and a door.
  const capH = Math.min(floorY - wallY - 1, HALL_H + 2 * (4 + baysOf(core.brain ?? 0)));
  const capY = Math.max(wallY + 1, Math.round(hallY + HALL_H / 2 - capH / 2));
  const chamber = blank("__brain", "The Brain", [{ x: MX + 1, y: capY, w: chamberW, h: capH }], {
    brain: true,
    open: true,
    kind: "chamber",
    floor: "floor",
  });
  {
    const cx = MX + 1 + Math.floor(chamberW / 2);
    const cy = Math.floor((wallY + floorY) / 2);
    const top = capY;
    const bot = capY + capH - 1;
    chamber.props.push({ x: cx, y: cy - 2, tile: "core", live: "screen" });
    // A colonnade down both sides, every third row, so the chamber is HELD UP rather than shut
    // in — the one space in the plan that is not four walls and a door.
    for (let y = top + 2; y < bot - 1; y += 3) {
      chamber.props.push({ x: MX + 2, y, tile: "pillar" });
      chamber.props.push({ x: MX + chamberW - 1, y, tile: "pillar" });
    }
    chamber.props.push({ x: cx - 3, y: cy - 5, tile: "plant" });
    chamber.props.push({ x: cx + 3, y: cy - 5, tile: "plant" });
    // A bench for every place in front of it, and the tables BETWEEN them — a seat whose north
    // cell is a table is a person sitting on a table.
    for (let i = -1; i <= 1; i++) chamber.props.push({ x: cx + i * BAY, y: cy + 4, tile: "bench" });
    chamber.props.push({ x: cx - 1, y: cy + 4, tile: "tableM" });
    chamber.props.push({ x: cx + 1, y: cy + 4, tile: "tableM" });
    chamber.props.push({ x: cx - 3, y: top + 1, tile: "board", live: "screen" });
    chamber.props.push({ x: cx + 3, y: bot - 1, tile: "crates" });
    const bays = baysOf(core.brain ?? 0);
    const lo = MX + 3;
    const hi = MX + chamberW - 2;
    const fits = (x: number): boolean => x >= lo && x <= hi;
    // The dais is the working end; the benches under the colonnade are where you sit when the
    // work is done. Same rule as every department — the chamber simply has no walls.
    /* The chamber has no desks and never had: it is a colonnade round a raised dais, which is the
       org chart drawn as architecture. So its working places are STANDING places — nothing to sit
       at, and a body drawn seated on a dais is a body sitting on the floor. */
    chamber.seats.push({ x: cx, y: cy, up: true, post: "desk", face: 1, perch: false });
    for (let i = 1; i < bays + 1; i++) {
      if (fits(cx - i * BAY)) {
        chamber.seats.push({ x: cx - i * BAY, y: cy, up: true, post: "desk", face: 1, perch: false });
      }
      if (fits(cx + i * BAY)) {
        chamber.seats.push({ x: cx + i * BAY, y: cy, post: "desk", face: -1, perch: false });
      }
    }
    for (let i = -1; i <= 1; i++) {
      const x = cx + i * BAY;
      if (fits(x)) chamber.seats.push({ x, y: cy + 5, post: "rest", face: x < cx ? 1 : -1 });
    }
    chamber.dais = { x: cx - 2, y: cy - 3, w: 5, h: 3 };
  }
  rooms.push(chamber);

  layBand(north, "n");
  layBand(south, "s");

  // The front desk, inside the gate. Open like the chamber, because a lobby with a door on it is
  // not a lobby.
  const lobH = Math.min(floorY - wallY - 1, HALL_H + 2 * (4 + Math.max(MIN_BAYS, core.lobby ?? 0)));
  const lobY = Math.max(wallY + 1, Math.round(hallY + HALL_H / 2 - lobH / 2));
  const lobby = blank("", "Front desk", [{ x: lobbyX, y: lobY, w: LOBBY_W, h: lobH }], {
    lobby: true,
    open: true,
    kind: "lobby",
    floor: "floor",
  });
  {
    const lx = lobbyX + 1;
    lobby.props.push({ x: lx + 1, y: gateY - 4, tile: "plant" });
    lobby.props.push({ x: lx + 4, y: gateY - 4, tile: "sofa" });
    lobby.props.push({ x: lx + 1, y: gateY + 4, tile: "sofa" });
    lobby.props.push({ x: lx + 4, y: gateY + 4, tile: "plant" });
    const only = uniques.pick(["cooler", "vending", "coffee", "tank"]);
    if (only) lobby.props.push({ x: lx + 5, y: gateY - 2, tile: only, live: LIVE_OF[only] });
    lobby.props.push({ x: lx, y: lobY + 1, tile: "board", live: "screen" });
    lobby.props.push({ x: lx + 5, y: lobY + lobH - 2, tile: "plant" });
    /* ONE desk and ONE couch per head, not one per pair: the lobby is where the discovered
       sessions and the department-less land, and most of them arrive FINISHED or FOREIGN — all
       of them wanting the waiting side at once. A rest end sized to half of them pushed the
       other half onto the counter, where a finished body is indistinguishable from a ready one. */
    const places = Math.max(MIN_BAYS, core.lobby ?? 0);
    // A front desk is people BEHIND a counter and people waiting in front of it. The waiting
    // side is the rest end, and it is what a visitor from another project is shown to.
    // The counter, and the waiting side. Each place has the thing it belongs to directly behind
    // it, which is the whole rule: a seat is a seat because of what is at its back.
    for (let i = 0; i < places; i++) {
      const y = gateY - 3 + i * 3;
      if (y + 1 >= lobY + lobH - 1) break;
      lobby.props.push({ x: lx + 1, y, tile: "desk", live: i % 2 ? undefined : "screen" });
      lobby.seats.push({ x: lx + 1, y: y + 1, up: i === 0, post: "desk", face: 1 });
    }
    for (let i = 0; i < places; i++) {
      const y = lobY + 2 + i * PITCH;
      if (y >= lobY + lobH - 1) break;
      lobby.props.push({ x: lx + 5, y: y - 1, tile: i % 2 ? "bench" : "sofa" });
      lobby.seats.push({ x: lx + 5, y, post: "rest", face: -1 });
    }
    lobby.props.push({ x: lx + 4, y: lobY + 2, tile: "tableM" });
  }
  rooms.push(lobby);

  // Outdoors. Not a room and no walls: you get there by walking out of the gate, which is the
  // only reason the gate exists.
  /* OUT ON THE WEB. Two ranks on a five-by-three lattice of lawn was the outdoor half of the
     placement complaint: nothing anchored anybody, so a fetch agent stood in the middle of a
     field. Now the yard has a PATH out of the gate with working places along it, and a bench
     under the trees to the south that is the yard's rest end. */
  /* Sized to the people who actually go out: a fixed two-and-two for five web agents pushed the
     overflow into a queue below the world, standing in the blocked grounds. Rows of standing
     places march NORTH up the path, rows of benches march SOUTH under the trees, until both
     ends hold the whole outdoor headcount — the same both-ends rule as every indoor room. */
  const yardSeats: Seat[] = [];
  const yardProps: Prop[] = [];
  const perRow = Math.ceil((OUT - 2) / PITCH);
  const yardRows = Math.max(1, Math.ceil((core.web ?? 0) / perRow));
  for (let r = 0; r < yardRows; r++) {
    const y = gateY - 1 - r * PITCH;
    if (y < wallY + 6) break; /* the mast and the treeline own the top of the yard */
    for (let dx = 2; dx < OUT; dx += PITCH) {
      // Nothing to sit on out here: an agent working the web is standing on the path looking out.
      yardSeats.push({ x: facadeX + dx, y, up: true, post: "desk", face: 1, perch: false });
    }
  }
  for (let r = 0; r < yardRows; r++) {
    const benchY = gateY + 3 + r * (PITCH + 1);
    if (benchY + 1 > floorY - 5) break; /* the pine and the blooms own the bottom corner */
    for (let dx = 2; dx < OUT; dx += PITCH) {
      // The bench first, the place in front of it second. Benches laid on their own rhythm put two
      // of the three places on bare grass, which is the "sitting on a lawn" defect one room out.
      yardProps.push({ x: facadeX + dx, y: benchY, tile: "benchPark" });
      yardSeats.push({ x: facadeX + dx, y: benchY + 1, post: "rest", face: dx < OUT / 2 ? 1 : -1 });
    }
    for (let dx = 3; dx < OUT; dx += PITCH) yardProps.push({ x: facadeX + dx, y: benchY, tile: "tableM" });
  }
  const yard = blank("__web", "Out on the web", [{ x: facadeX + 1, y: wallY, w: OUT, h: floorY - wallY + 1 }], {
    outdoor: true,
    open: true,
    kind: "outside",
    floor: "grass",
    seats: yardSeats,
    props: [
      ...yardProps,
      { x: facadeX + 4, y: wallY + 2, tile: "mast" },
      { x: facadeX + 2, y: wallY + 4, tile: "tree" },
      { x: facadeX + 6, y: floorY - 3, tile: "pine" },
      { x: facadeX + 5, y: wallY + 3, tile: "planter" },
      { x: facadeX + 1, y: floorY - 4, tile: "blooms" },
    ],
  });
  rooms.push(yard);

  /* ── the walkability grid ────────────────────────────────────────────────────────────────
     Solid by default INSIDE the envelope: the building is mass until something carves a space
     out of it, which is the opposite of the old plan (empty until something drew a wall) and is
     why a shallow room now leaves visible structure behind it rather than a blank corridor. */
  const solid = new Array<boolean>(cols * rows).fill(true);
  const carve = (r: Rect): void => {
    for (let y = r.y; y < r.y + r.h; y++) {
      for (let x = r.x; x < r.x + r.w; x++) {
        if (x < 0 || y < 0 || x >= cols || y >= rows) continue;
        solid[y * cols + x] = false;
      }
    }
  };
  const block = (x: number, y: number): void => {
    if (x < 0 || y < 0 || x >= cols || y >= rows) return;
    solid[y * cols + x] = true;
  };
  /* A prop blocks its whole GROUND footprint, not only its anchor: a two-seat couch is two
     cells, a piano and a parked car are two wide. One place, or the wide furniture is solid on
     paper and walk-through in pixels. */
  const blockProp = (p: Prop): void => {
    for (const [dx, dy] of footprint(p.tile)) block(p.x + dx, p.y + dy);
  };

  const hall: Rect = { x: bandStart - 1, y: hallY, w: lobbyX - bandStart + 2, h: HALL_H };
  const hallProps: Prop[] = [];
  const scenery: Prop[] = [];
  /* The runner runs the WHOLE spine, from the front gate to the dais at the head of the chamber,
     and is drawn over the rooms rather than under them — a corridor that stops at the chamber
     door is a corridor that leads nowhere. */
  const runnerRect: Rect = { x: MX + 1 + Math.floor(chamberW / 2), y: hallY + 1, w: facadeX - (MX + 1 + Math.floor(chamberW / 2)), h: 1 };
  const envelope: Rect = { x: MX, y: wallY, w: facadeX - MX + 1, h: floorY - wallY + 1 };
  const runner: Rect = runnerRect;
  carve(hall);

  // Things standing IN the passage. A corridor with nothing in it is a gap between rooms. Never
  // in a door's own column: the door cell's only way out is the hall row directly under it, so a
  // prop there walls a whole department in.
  {
    const blocked = new Set<number>();
    for (const r of rooms) for (const d of r.doors) for (let k = -1; k <= 1; k++) blocked.add(d.x + k);
    /* No foliage in the corridor. Six floating-tree reports narrowed to one class — leafy decor
       on open floor beside standing bodies — and the corridor is the busiest open floor there
       is. An absent plant is invisible; a floating one is a bug report. */
    const kit: TileId[] = ["bench", "crates", "cabinet", "cooler", "printer"];
    let n = 0;
    for (let x = hall.x + 3; x < hall.x + hall.w - 3; x += 5) {
      if (blocked.has(x)) continue;
      const tile = uniques.pick([kit[n % kit.length], "bench", "crates"]) ?? "bench";
      hallProps.push({ x, y: n % 2 === 0 ? hallY : hallY + HALL_H - 1, tile, live: LIVE_OF[tile] });
      n++;
    }
  }

  /* ── DECOR YIELDS TO FURNITURE ────────────────────────────────────────────────────────────
     "Trees are still floating" had a SECOND mechanism the shadow work never touched, found only
     by rendering the real registry: a potted plant sharing a cell with a whiteboard in the
     chamber, and a plant standing directly NORTH of the front desk. The painter draws south
     over north, so the southern prop swallows the plant's pot and trunk and the CANOPY appears
     to grow out of the furniture — a tree planted on a desk. Equipment north of a desk is a
     deliberate composition (a globe DISPLAYED on it); foliage is not, because foliage claims to
     stand in soil. So: a leafy prop never shares a cell with another prop and never stands
     directly north of one. The plant is the thing dropped — furniture is load-bearing, decor is
     not. Grounds are exempt (a copse is touching crowns by design), and this runs BEFORE the
     solidity pass so a dropped plant does not leave an invisible obstacle behind. */
  const LEAFY_DECOR = new Set<TileId>(["plant", "tree", "treeBig", "pine", "bush"]);
  const scrub = (props: Prop[]): Prop[] => {
    const firm = new Set(
      props.filter((p) => !LEAFY_DECOR.has(p.tile)).map((p) => p.x + ":" + p.y),
    );
    return props.filter(
      (p) =>
        !LEAFY_DECOR.has(p.tile) ||
        (!firm.has(p.x + ":" + p.y) && !firm.has(p.x + ":" + (p.y + 1))),
    );
  };
  for (const room of rooms) room.props = scrub(room.props);
  const scrubbedHall = scrub(hallProps);
  hallProps.length = 0;
  for (const p of scrubbedHall) hallProps.push(p);

  /* ── AND BODIES YIELD TO DECOR ────────────────────────────────────────────────────────────
     The scrub above stops a plant growing out of FURNITURE; the sixth report found the same
     picture made by a PERSON. A body is two tiles tall and drawn over the map, so anybody ON
     the cell directly south of a leafy prop swallows its pot and trunk and the canopy floats
     over their head — measured at 94.8% sprite overlap for a body merely WALKING THROUGH, so a
     stop-only ban was not enough: at walk speed the fusion still holds for whole frames.

     So the pot claims its floor space outright: the cell south of every surviving indoor leafy
     prop is SOLID, exactly as the furniture's own footprint is, and the router walks around a
     potted tree the way it walks around a desk. Declared seats there are dropped first so the
     probe reports a seat-vs-decor clash rather than a seat in masonry. Outdoors is exempt: a
     copse is terrain, trees carry trunks, litter and shadows, and the lawn has room to pass. */
  /* The yard is NOT exempt: it is an outdoor ROOM with real workers walking it, and its two
     trees sat outside the first pass — a web agent stopping south of the pine wore its canopy,
     the same fusion indoors was cured of. The exemption belongs to the GROUNDS (scenery, where
     nobody can walk at all), not to outdoor floor. */
  const shy = new Set<number>();
  for (const p of [...rooms.flatMap((r) => r.props), ...hallProps]) {
    if (LEAFY_DECOR.has(p.tile)) shy.add((p.y + 1) * cols + p.x);
  }
  for (const room of rooms) {
    room.seats = room.seats.filter((s) => !shy.has(s.y * cols + s.x));
  }

  // Every room's own shape, and the same pass that hands the renderer its rectangles.
  for (const room of rooms) {
    if (room.open) {
      carve(room.rects[0]);
      room.floorRuns = [{ ...room.rects[0] }];
      /* A STANDING PLACE OUTRANKS A PROP HERE TOO. The rule below was written for rooms with
         walls and quietly skipped every open one — the lobby, the chamber, the plazas, the yard —
         so the front desk's vending machine was standing in the middle of the waiting bench and
         the seat under it was simply solid. A rule that holds for four room kinds and not the
         other four is not a rule. */
      const on = new Set(room.seats.map((p) => p.x + ":" + p.y));
      room.props = room.props.filter((p) => !on.has(p.x + ":" + p.y));
      for (const p of room.props) blockProp(p);
      continue;
    }
    const shape = shapeOf(room.rects, cols);
    for (const key of shape.floor) solid[key] = false;
    for (const key of shape.wall) solid[key] = true;
    for (const d of room.doors) {
      solid[d.y * cols + d.x] = false;
      shape.wall.delete(d.y * cols + d.x);
      shape.face.delete(d.y * cols + d.x);
      if (d.deep) {
        solid[(d.y + 1) * cols + d.x] = false;
        shape.wall.delete((d.y + 1) * cols + d.x);
        shape.face.delete((d.y + 1) * cols + d.x);
      }
    }
    room.floorRuns = runsOf(shape.floor, shape.box, cols);
    room.faceRuns = runsOf(shape.face, shape.box, cols);
    const body = new Set([...shape.wall].filter((k) => !shape.face.has(k)));
    room.wallRuns = runsOf(body, shape.box, cols);

    /* A STANDING PLACE OUTRANKS A PROP, and it is enforced here rather than hoped for in the
       furnisher.
       `furnish` lays out desks, seats and clutter against the room's first rectangle on a fixed
       inset, which is exactly right while every room IS that rectangle. The moment a plan form
       steps the wall, some of those cells are masonry or are already carrying a crate — and a
       body standing inside a wall is invisible until an invariant goes looking for it (this one
       did: three seats, three departments, no visible symptom). So: anything standing on a seat
       is removed, and a seat that is not floor walks to the nearest cell that is. */
    const seatAt = new Set(room.seats.map((p) => p.x + ":" + p.y));
    room.props = room.props.filter((p) => !seatAt.has(p.x + ":" + p.y));
    const propAt = new Set(room.props.map((p) => p.x + ":" + p.y));
    const taken = new Set<string>();
    for (const seat of room.seats) {
      const free = (x: number, y: number): boolean =>
        shape.floor.has(y * cols + x) && !propAt.has(x + ":" + y) && !taken.has(x + ":" + y);
      if (!free(seat.x, seat.y)) {
        let best: [number, number] | null = null;
        let near = Infinity;
        for (const key of shape.floor) {
          const x = key % cols;
          const y = (key - x) / cols;
          if (!free(x, y)) continue;
          const d = Math.abs(x - seat.x) + Math.abs(y - seat.y);
          if (d < near) { near = d; best = [x, y]; }
        }
        if (best) { seat.x = best[0]; seat.y = best[1]; }
      }
      taken.add(seat.x + ":" + seat.y);
    }

    // Furniture is solid: the moment a route has to bend around a desk the floor stops being a
    // backdrop and becomes a place with things in it. A prop on the wall FACE is already solid.
    for (const p of room.props) blockProp(p);
  }

  // The envelope, the grounds outside it, and the gate through the facade. Everything beyond the
  // building is solid: it is scenery the camera can rest on, never floor anybody walks.
  for (let x = 0; x < cols; x++) {
    for (let y = 0; y < rows; y++) {
      if (y < envelope.y || y >= envelope.y + envelope.h || x < envelope.x) block(x, y);
    }
  }
  for (let x = envelope.x; x <= facadeX; x++) {
    block(x, envelope.y);
    block(x, envelope.y + envelope.h - 1);
  }
  for (let y = envelope.y; y < envelope.y + envelope.h; y++) {
    block(envelope.x, y);
    block(facadeX, y);
  }
  carve({ x: facadeX + 1, y: envelope.y, w: OUT, h: envelope.h });
  solid[gateY * cols + facadeX] = false;
  /* That carve just wiped the yard's own furniture blocks — the room pass ran first, so every
     bench, mast and tree out here was walkable-over and a body could stand INSIDE the pine.
     The critic's probe read it directly: ownSolid=0 on both yard trees. Blocked again, after
     the last carve that touches the strip. */
  for (const p of yard.props) blockProp(p);

  // What is left solid inside the envelope, as rectangles, so the renderer can draw the building's
  // mass rather than leaving a hole where a shallow room did not reach the outside wall.
  const massCells = new Set<number>();
  for (let y = envelope.y; y < envelope.y + envelope.h; y++) {
    for (let x = envelope.x; x < facadeX; x++) if (solid[y * cols + x]) massCells.add(y * cols + x);
  }
  for (const room of rooms) {
    for (const r of [...room.wallRuns, ...room.faceRuns]) {
      for (let x = r.x; x < r.x + r.w; x++) massCells.delete(r.y * cols + x);
    }
    for (const p of room.props) massCells.delete(p.y * cols + p.x);
  }
  const mass = runsOf(massCells, envelope, cols);

  for (const p of hallProps) blockProp(p);

  /* The pot's floor space, claimed LAST — after every carve, or the room-shape pass would hand
     the cell straight back to the walkable floor it was cut from. */
  for (const cell of shy) solid[cell] = true;

  /* ── THE CLEARANCE NET ────────────────────────────────────────────────────────────────────
     The emitters above each refuse the door approach, and this is the guarantee they cannot
     give: after EVERY solidity pass, every seat in a walled room must be walkable from its own
     door. Two rooms on a real roster proved that prose rules over five emitters do not compose —
     a plant's claimed floor cell and a walked feature sealed two departments shut, invisibly,
     because each placement was locally legal. When a seat is cut off, the least load-bearing
     prop on the boundary of the reachable region is removed (never a piece a seat sits against,
     never the wall face) until the room opens. Furniture is furniture; a route is the point. */
  for (const room of rooms) {
    if (room.open || room.outdoor || !room.doors.length) continue;
    const inRoom = (x: number, y: number): boolean =>
      room.rects.some((r) => x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h);
    const reach = (): Set<number> => {
      const seen = new Set<number>();
      const q: number[] = [];
      for (const d of room.doors) {
        const k = d.y * cols + d.x;
        seen.add(k);
        q.push(k);
      }
      for (let head = 0; head < q.length; head++) {
        const cur = q[head];
        const x = cur % cols;
        const y = (cur - x) / cols;
        for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
          const nx = x + dx;
          const ny = y + dy;
          if (!inRoom(nx, ny)) continue;
          const k = ny * cols + nx;
          if (solid[k] || seen.has(k)) continue;
          seen.add(k);
          q.push(k);
        }
      }
      return seen;
    };
    const backing = new Set(room.seats.map((s) => s.x + ":" + (s.y - 1)));
    for (let guard = 0; guard < 24; guard++) {
      const seen = reach();
      if (room.seats.every((s) => seen.has(s.y * cols + s.x))) break;
      let removed = false;
      for (let i = room.props.length - 1; i >= 0; i--) {
        const p = room.props[i];
        if (backing.has(p.x + ":" + p.y)) continue;
        if (p.y <= room.y + 1) continue; /* the wall face is masonry, not an obstruction */
        const cells = footprint(p.tile).map(([dx, dy]) => (p.y + dy) * cols + (p.x + dx));
        const podium = (p.y + 1) * cols + p.x; /* a leafy prop's claimed floor space */
        const all = shy.has(podium) ? [...cells, podium] : cells;
        const onEdge = all.some((k) => {
          const x = k % cols;
          const y = (k - x) / cols;
          return (
            seen.has(y * cols + x - 1) || seen.has(y * cols + x + 1) ||
            seen.has((y - 1) * cols + x) || seen.has((y + 1) * cols + x)
          );
        });
        if (!onEdge) continue;
        for (const k of all) solid[k] = false;
        shy.delete(podium);
        room.props.splice(i, 1);
        removed = true;
        break;
      }
      if (!removed) break;
    }
  }

  /* ── the grounds ──────────────────────────────────────────────────────────────────────────
     What was here before is the whole of "floating trees, and the sprites are weirdly placed":

       for (y += 2) for (x += 3) if (hash % 5) continue;  →  plant or urn

     Three defects in four lines. The KIT is indoor — `plant` is a houseplant and `urn` is a
     screen on a stand, so a lawn was furnished with office decor. The LATTICE is a hard grid of
     three columns by two rows, so every piece lands in a visible rank and file, which is exactly
     what "weirdly placed" looks like from far enough back to see the pattern. And the DENSITY is
     uniform, so the grounds are the same everywhere and read as wallpaper.

     A landscape is none of those things. It is CLUSTERED (trees grow in copses, and a copse is
     what makes the gaps between them read as clearings), it is GRADED (thick at the boundary of
     the site, thin where people walk), and it has more than one kind of thing in it. So:

       - a made PATH out of the gate, with lamps along it, because the way in should look like a
         way in and not a gap in a wall;
       - copses seeded at jittered centres, each one a handful of pieces scattered around it —
         the cluster is the unit, never the cell;
       - a thicker fringe at the edges of the site, which is what stops the world's border being
         a straight line where the drawing simply stops;
       - a pond, some rock, some flowers, and tufts of longer grass at low density so the lawn
         has texture the ground pattern alone cannot carry at this pitch.

     All deterministic in the cell, so the site looks the same on every render. */
  const taken = new Set<string>();
  const paved = new Set<string>();
  /** Cells a TRUNK must keep out of, because its shadow would reach the water from there.
   *  Declared with the other occupancy sets rather than inside the pond, so `plant` below reads
   *  it as one of the site's standing rules and not as a special case for one feature. */
  const wet = new Set<string>();
  /** Inside the site and outside the building. */
  const site = (x: number, y: number): boolean => {
    if (x < 1 || y < 1 || x >= cols - 1 || y >= rows - 1) return false;
    // Never on the building, never in the yard, never on the way out of the gate.
    return !(x >= envelope.x - 1 && x <= facadeX + OUT + 1 && y >= envelope.y - 1 && y <= envelope.y + envelope.h);
  };
  /** Free for something to STAND on. Ground is a separate layer: a tree may grow in a meadow. */
  const clear = (x: number, y: number): boolean => site(x, y) && !taken.has(x + ":" + y);
  /* WIND COSTS A WEB ANIMATION PER PLANT, and a planted site is four hundred plants. Every one of
     them swaying is five hundred running animations for a lawn — measured, against about seventy
     for the entire building before any of this existed — and the ones that cost the most are the
     ones worth the least: a tuft of grass eight pixels tall moving by one of them is invisible at
     any scale anybody reads this at.
     So the wind is in the CANOPIES, and in one crown out of three. A wood where every tree moves
     together is a texture scrolling; a wood where some of them move is wind. */
  const WINDY = new Set<TileId>(["tree", "treeBig", "pine", "bush"]);
  const TALL = new Set<TileId>(["tree", "treeBig", "pine", "lamppost", "mast"]);
  const plant = (x: number, y: number, tile: TileId, live?: LiveId): void => {
    if (!clear(x, y)) return;
    if (TALL.has(tile) && wet.has(x + ":" + y)) return;
    taken.add(x + ":" + y);
    // No sway anywhere, indoors or out: "there are plants still moving inside" was the eighth
    // report of this one thing, and a plant has no state to express. `live` stays in the
    // signature because equipment (screens, fans) still earns it.
    scenery.push({ x, y, tile, live });
  };

  /* ── the ground itself ────────────────────────────────────────────────────────────────────
     Props alone cannot furnish a site. A wall-to-wall lawn with objects on it is a green sheet
     with objects on it, because every prop is one tile and one tile is too small to be read as a
     SHAPE at any scale where the whole company is in frame — which is precisely the scale the
     camera exists to reach. What is big enough is the GROUND, so the site gets ground of more
     than one kind: patches of meadow left unmown, and bare earth where things are walked.

     Laid as ellipses around deterministic centres, which is a shape rather than a rectangle and
     costs one cell each. They go down FIRST, so everything planted afterwards stands on them. */
  const soil = new Map<TileId, Set<number>>();
  const lay = (x: number, y: number, tile: TileId): void => {
    for (const cells of soil.values()) cells.delete(y * cols + x);
    let cells = soil.get(tile);
    if (!cells) soil.set(tile, (cells = new Set()));
    cells.add(y * cols + x);
  };
  const ground = (cx: number, cy: number, rx: number, ry: number, tile: TileId): void => {
    for (let y = cy - ry; y <= cy + ry; y++) {
      for (let x = cx - rx; x <= cx + rx; x++) {
        const nx = (x - cx) / rx;
        const ny = (y - cy) / ry;
        // A ragged edge, not a drawn ellipse: the boundary cells drop out on their own hash.
        const d = nx * nx + ny * ny;
        if (d > 1) continue;
        if (d > 0.55 && hash32("e:" + x + ":" + y) % 3 === 0) continue;
        // GROUND IS A LAYER, not an occupant: a meadow that blocked planting would leave every
        // patch on the site conspicuously treeless, which is the opposite of what a meadow is.
        if (!site(x, y)) continue;
        paved.add(x + ":" + y);
        lay(x, y, tile);
      }
    }
  };
  for (let gy = 3; gy < rows; gy += 9) {
    for (let gx = 3; gx < cols; gx += 11) {
      const seed = hash32("m:" + gx + ":" + gy);
      if (seed % 3 === 0) continue;
      ground(gx + (seed % 6), gy + ((seed >>> 4) % 6), 3 + (seed % 4), 2 + ((seed >>> 8) % 3), "meadow");
    }
  }

  /* THE STREET. The way out of the gate is a real road now — the register the urban pack's own
     sample town sets: asphalt with lane dashes, a kerb of pavement either side, a crosswalk
     where the yard path meets it, street lights along the kerb and a few parked cars. "Out on
     the web" stops being a sliver of lawn and becomes the curb the web workers stand on — the
     loudest thing on the east side of the map, which is what findability actually is. */
  const roadX = facadeX + OUT + 1;
  const pathY = gateY;
  for (let y = pathY - 3; y <= pathY + 4; y++) {
    for (let x = roadX; x < cols; x++) {
      if (y === pathY - 3 || y === pathY + 4) lay(x, y, "path");
      else if (x === roadX + 1) lay(x, y, "roadCross");
      else if (y === pathY + 1) lay(x, y, x % 2 === 0 ? "roadDash" : "road");
      else lay(x, y, "road");
      taken.add(x + ":" + y);
    }
  }
  for (let x = roadX + 3; x < cols - 1; x += 6) {
    plant(x, pathY - 4, "lamppost");
    plant(x + 3, pathY + 5, "lamppost");
  }
  /* Parked on the road's south lane. The road cells are already `taken` (they are road), so the
     cars are pushed directly — the lane is theirs by construction, and `clear()` would refuse
     every spot on it. */
  {
    const CARS: TileId[] = ["taxi", "carRed", "van"];
    for (let i = 0, x = roadX + 3; x + 1 < cols; x += 4, i++) {
      scenery.push({ x, y: pathY + 2, tile: CARS[(i + hash32("car:" + x)) % CARS.length] });
    }
  }
  /* The street furniture a town corner has, where the yard path meets the road. */
  plant(roadX - 1, pathY - 2, "hydrant");
  plant(roadX - 1, pathY + 3, "mailbox");
  plant(roadX + 2, pathY - 3, "urn");

  /* ── THE POND ─────────────────────────────────────────────────────────────────────────────
     Laid BEFORE anything is planted, because water is the one thing on the site that decides
     where other things may go.

     The first one was a hand-listed set of cells with an independently-drawn ellipse of earth
     near it, and it rendered as exactly that: a blocky plus of blue with a differently-blocky
     plus of brown offset from it, and a tree on the lip throwing its shadow across the water. Two
     rules fix all of it, and both are about the water OWNING its surroundings rather than sharing
     a neighbourhood with them:

       THE BANK IS DERIVED FROM THE WATER, never drawn beside it — it is exactly the cells that
       TOUCH water, so a shore hugs its own pond whatever shape the pond is.
       NOTHING TALL STANDS WITHIN REACH OF THE WATER. A tree's shadow rakes four cells to the
       right, so a tree planted on the shore lays a hard slab across the surface — which is what
       "floating" looks like when the thing under the shadow is flat and blue. Low cover is fine
       and welcome; trunks keep back. */
  {
    const cx = envelope.x + 6;
    const cy = envelope.y + envelope.h + 4;
    const rx = 4;
    const ry = 3;
    const water: [number, number][] = [];
    for (let y = cy - ry; y <= cy + ry; y++) {
      for (let x = cx - rx; x <= cx + rx; x++) {
        const nx = (x - cx) / rx;
        const ny = (y - cy) / ry;
        const d = nx * nx + ny * ny;
        if (d > 1) continue;
        if (d > 0.6 && hash32("w:" + x + ":" + y) % 4 === 0) continue;
        if (!site(x, y)) continue;
        water.push([x, y]);
      }
    }
    const isWater = new Set(water.map(([x, y]) => x + ":" + y));
    for (const [x, y] of water) {
      lay(x, y, "pond");
      taken.add(x + ":" + y);
    }
    for (const [x, y] of water) {
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          const bx = x + dx;
          const by = y + dy;
          if (isWater.has(bx + ":" + by) || !site(bx, by)) continue;
          lay(bx, by, "earth");
          taken.add(bx + ":" + by);
        }
      }
    }
    /* How far a trunk keeps back from the water. The projected-shadow system this was derived
       from is gone (the Kenney art grounds itself), but an open shore is still the difference
       between a pond and a puddle in a wood. */
    const KEEP = 5;
    const RIM = 2;
    for (const [x, y] of water) {
      for (let dy = -RIM; dy <= RIM; dy++) {
        for (let dx = -KEEP; dx <= 1; dx++) wet.add(x + dx + ":" + (y + dy));
      }
    }
  }

  /* Copses. A centre every so often, jittered off its own lattice cell so the centres themselves
     are not in rank and file, then a handful of pieces scattered around each one — bigger trees
     toward the middle, shrubs and tufts at the skirt, which is how a stand of trees actually
     thins out. */
  const WOOD: TileId[] = ["tree", "treeBig", "pine", "tree", "pine", "treeBig"];
  const SKIRT: TileId[] = ["bush", "tuft", "blooms", "bush", "rock", "tuft"];
  for (let gy = 2; gy < rows - 2; gy += 6) {
    for (let gx = 2; gx < cols - 2; gx += 7) {
      const seed = hash32("copse:" + gx + ":" + gy);
      if (seed % 6 === 0) continue; /* a clearing */
      const cx = gx + (seed % 5);
      const cy = gy + ((seed >>> 3) % 5);
      // Thicker at the boundary of the site than in the middle of the lawn.
      const edge = Math.min(cx, cy, cols - 1 - cx, rows - 1 - cy);
      const size = 3 + ((seed >>> 7) % 3) + (edge < 4 ? 3 : 0);
      /* A copse is TOUCHING crowns, not a sprinkle. Jittering each piece independently inside a
         five-by-five box scatters them one cell apart and every tree ends up alone, which at this
         tile size reads as a shrub — the mass has to be adjacent before the eye calls it a tree.
         So the shape grows OUTWARD from the centre along a fixed spiral of neighbours, and only
         what falls off the end of it lands in the skirt. */
      const SPIRAL = [
        [0, 0], [1, 0], [0, 1], [1, 1], [-1, 0], [0, -1], [-1, 1], [1, -1],
        [2, 0], [-1, -1], [2, 1], [0, 2], [1, 2], [-2, 0], [2, -1], [-1, 2],
      ];
      for (let k = 0; k < size && k < SPIRAL.length; k++) {
        const s = hash32("t:" + cx + ":" + cy + ":" + k);
        const [ox, oy] = SPIRAL[k];
        const core = Math.abs(ox) + Math.abs(oy) <= 1;
        const kit = core ? WOOD : SKIRT;
        const tile = kit[(s >>> 9) % kit.length];
        plant(cx + ox, cy + oy, tile);
      }
    }
  }

  /* A TREE LINE at the boundary. Without it the site simply stops: the outermost copse is
     followed by lawn to the edge of the drawing, and the edge of the drawing is a straight line
     nothing in the picture accounts for. A planted boundary is what a site has instead. */
  for (let x = 1; x < cols - 1; x++) {
    for (const y of [1, 2, rows - 2, rows - 3]) {
      const seed = hash32("edge:" + x + ":" + y);
      if (seed % 3) continue;
      const tile = (["treeBig", "tree", "pine", "tree"] as TileId[])[(seed >>> 6) % 4];
      plant(x, y, tile);
    }
  }
  for (let y = 1; y < rows - 1; y++) {
    for (const x of [1, 2, cols - 2, cols - 3]) {
      const seed = hash32("edge:" + x + ":" + y);
      if (seed % 3) continue;
      const tile = (["pine", "tree", "treeBig", "bush"] as TileId[])[(seed >>> 6) % 4];
      plant(x, y, tile);
    }
  }

  /* And the thin stuff. Uniform noise over the whole site is the lattice defect wearing a
     different hat — every cell tested at the same rate gives an even sprinkle of identical marks,
     which is wallpaper. It goes where cover actually grows: at the SKIRT of what is already
     planted, so a copse has a fringe and the open lawn stays open. */
  const rim: Prop[] = [];
  const CANOPY = new Set<TileId>(["tree", "treeBig", "pine"]);
  for (const at of scenery) {
    // Only under the CANOPIES, and at half the old rate. Cover is the cheapest thing on the site
    // to look at and the most expensive to draw — two hundred sprites of texture measured as a
    // fifth of the whole scene's node count — so it goes where it actually reads: as the skirt of
    // a copse, never as an even sprinkle over open lawn.
    if (!CANOPY.has(at.tile)) continue;
    for (const [ox, oy] of [[-1, 1], [1, 1], [2, 0], [0, 2]]) {
      const x = at.x + ox;
      const y = at.y + oy;
      const seed = hash32("g:" + x + ":" + y);
      if (seed % 4) continue;
      if (!clear(x, y)) continue;
      taken.add(x + ":" + y);
      // No wind down here. Ground cover is eight pixels tall and moving one of them is an
      // animation nobody can see, two hundred times over.
      rim.push({ x, y, tile: seed % 6 === 0 ? "blooms" : "tuft" });
    }
  }
  scenery.push(...rim);

  /* ── THE FOREST FLOOR ─────────────────────────────────────────────────────────────────────
     The fourth report of "floating trees" was made at MAP scale, and at map scale the shadow
     system cannot answer it: a cast shadow spans three cells, a cell at the whole-floor rung is
     eight device pixels, and no alpha makes three pixels read as contact. What DOES read at every
     rung is GROUND — the one mark on a site bigger than a prop — so every canopy stands on a
     patch of forest floor: its own cell plus a ring biased SOUTH-EAST, the side its shadow
     already claims, unioned across a copse into one organic clearing. Grass does not grow under
     a dense crown; now the drawing says so, and a tree is attached to the earth before a single
     shadow pixel is spent. */
  const soilAt = (x: number, y: number): TileId | null => {
    for (const [kind, cells] of soil) if (cells.has(y * cols + x)) return kind;
    return null;
  };
  for (const at of scenery) {
    if (!CANOPY.has(at.tile)) continue;
    for (const [ox, oy] of [[0, 0], [1, 0], [0, 1], [1, 1], [-1, 0], [0, -1], [-1, 1], [1, -1]]) {
      const x = at.x + ox;
      const y = at.y + oy;
      /* The core (the crown's own cell and its south-east contact) is always floored; the rim
         cells drop out on their own hash so the clearing has a ragged, grown edge rather than a
         drawn one. */
      const core = ox >= 0 && oy >= 0;
      if (!core && hash32("u:" + x + ":" + y) % 3 !== 0) continue;
      if (!site(x, y)) continue;
      const k = soilAt(x, y);
      // Never over water, its bank, or a made path — only lawn and meadow yield to the wood.
      if (k && k !== "meadow") continue;
      lay(x, y, "litter");
    }
  }

  const byId = new Map(rooms.map((r) => [r.id, r]));
  return {
    cols,
    rows,
    rooms,
    byId,
    solid,
    facadeX,
    gateY,
    brainRoom: chamber,
    lobby,
    yard,
    hall,
    runner,
    mass,
    envelope,
    hallProps,
    scenery,
    terrain: [...soil].map(([tile, cells]) => ({
      tile,
      runs: runsOf(cells, { x: 0, y: 0, w: cols, h: rows }, cols),
    })),
    shy: [...shy],
    street: { y: gateY - 3 },
  };
}
