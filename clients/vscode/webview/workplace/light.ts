/** LIGHT — turns a floor plan into a place somebody is standing in.
 *
 *  The version this replaces lit a room with a flat 10% hue wash, edge to edge — a tint, not
 *  light (that's what a diagram uses to mean "this region is category B"). It's also why LIGHT
 *  theme read as washed out while dark read as a room: dark got depth for free from a near-black
 *  background the mixes fell toward; light had nothing to fall to, so every surface landed in a
 *  12-point band and walls came out BRIGHTER than the floor they should occlude.
 *
 *  So value range comes from LIGHT AND SHADOW instead, the way every game that ever looked like a
 *  place does it:
 *
 *   - a room is DARK by default; lamps carve pools out of it. Pool quantised to the TILE GRID,
 *     four levels, no smooth ramp — a radial gradient rasterises at screen res and instantly
 *     un-pixels a pixel scene. Banded light on the grid is the signature, and what an 8-bit
 *     renderer could actually do — why it reads as a game, not CSS.
 *   - every wall casts. ONE global light direction (north-west, the oldest convention) so every
 *     wall drops a hard band south/east of it, and the room gets a visible corner.
 *   - every prop casts its own SILHOUETTE, not a blob: same drawing re-rendered through an
 *     all-ink palette, offset. One extra sheet entry per distinct prop, one use per placement.
 *
 *  Pool is TINTED by what the room is doing: rail says a run's state in word + colour from
 *  statusLanguage; world says the same state by the light's colour, legible across the map before
 *  any label resolves. Same taxonomy, two idioms.
 */
import type { Rect, Room, World } from "./world";

/** Cells per tile. Map's user units are cells; a tile is sixteen of them now the substrate is
 *  16x16 Kenney art. */
const C = 16;

/** Where light comes from in a room. Radius is in TILES. */
export interface Lamp {
  x: number;
  y: number;
  r: number;
  /** A fixture on the ceiling washes the room; a desk lamp or a screen is a hot spot in it. */
  kind: "fixture" | "spot";
}

/** Steps the pool quantises into. Four reads as falloff, few enough each band is a big visible
 *  shape, not a gradient in disguise. */
export const LEVELS = 4;

/** Band edges, fraction of lamp radius. Deliberately uneven — an even split makes the middle
 *  band dominate, and the pool reads as a disc with a rim. */
const EDGE = [0.3, 0.55, 0.78, 1];

/** The light in a room, as tile runs per level.
 *
 *  Index 0 is the brightest band; the LAST entry is everything no lamp reaches. Unlit set is
 *  returned, not left implicit: shade must paint only where light is NOT — a dark wash under the
 *  pools would need lightening back by them, and two stacked translucent fills is a gradient,
 *  the one thing this layer exists not to be.
 */
export function poolRuns(room: Room, lamps: readonly Lamp[], stride: number): Rect[][] {
  const floor = new Set<number>();
  for (const r of room.floorRuns) {
    for (let y = r.y; y < r.y + r.h; y++) for (let x = r.x; x < r.x + r.w; x++) floor.add(y * stride + x);
  }
  const level = new Map<number, number>();
  for (const key of floor) {
    const x = key % stride;
    const y = (key - x) / stride;
    let best = LEVELS;
    for (const l of lamps) {
      // Measured from the CENTRE of both tiles, so a lamp lights its own tile brightest, not the
      // corner it sits on.
      const d = Math.hypot(x - l.x, (y - l.y) * 1.15) / Math.max(0.5, l.r);
      let band = LEVELS;
      for (let i = 0; i < EDGE.length; i++) {
        if (d <= EDGE[i]) {
          band = i;
          break;
        }
      }
      // A spot is a hot centre: never contributes the outermost, flattest band, so a desk lamp
      // reads as small and bright, not a second wash over the room.
      if (l.kind === "spot" && band >= LEVELS - 1) band = LEVELS;
      if (band < best) best = band;
    }
    if (best < LEVELS) level.set(key, best);
  }
  const out: Rect[][] = [];
  for (let i = 0; i <= LEVELS; i++) {
    const cells = new Set<number>();
    if (i === LEVELS) for (const key of floor) { if (!level.has(key)) cells.add(key); }
    else for (const [key, v] of level) if (v === i) cells.add(key);
    out.push(runsOf(cells, room, stride));
  }
  return out;
}

/** Where a room's light actually hangs.
 *
 *  A fixture per rectangle of the footprint (an L gets two, so the alcove isn't a black hole),
 *  plus a hot spot on every emitting prop: screen, desk lamp, kettle's ring. Nothing placed by
 *  hand — a room furnished differently lights differently, same rule the furniture follows.
 */
export function lampsFor(room: Room): Lamp[] {
  const out: Lamp[] = [];
  for (const r of room.rects) {
    // A ROW of fixtures on a regular pitch, what a ceiling actually has. ONE lamp per room
    // produces a single vignette that reads as a tint again; several overlapping pools scallop,
    // and the scallop is what the eye reads as light.
    const pitch = 5;
    const nx = Math.max(1, Math.round(r.w / pitch));
    const ny = Math.max(1, Math.round(r.h / pitch));
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        out.push({
          x: r.x + ((i + 0.5) * r.w) / nx - 0.5,
          y: r.y + ((j + 0.5) * r.h) / ny - 0.5,
          r: Math.max(2.6, Math.min(4.4, (Math.min(r.w / nx, r.h / ny) * 0.86))),
          kind: "fixture",
        });
      }
    }
  }
  for (const p of room.props) {
    if (p.live === "screen") out.push({ x: p.x, y: p.y + 0.4, r: 2.1, kind: "spot" });
    else if (p.live === "lamp") out.push({ x: p.x, y: p.y + 0.8, r: 2.8, kind: "spot" });
    else if (p.live === "steam") out.push({ x: p.x, y: p.y + 0.5, r: 1.7, kind: "spot" });
  }
  return out;
}

/** Horizontal runs of a cell set inside a room's bounding box. Same merge the map does for
 *  tiles: one rectangle per run, so a lit room costs tens of nodes, not hundreds. */
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

/** The path for a set of tile runs, in cell units. */
export function runPath(runs: readonly Rect[]): string {
  return runs.map((r) => `M${r.x * C} ${r.y * C}h${r.w * C}v${r.h * C}h-${r.w * C}z`).join("");
}

/** How deep a wall's shadow falls onto the floor beside it, CELLS out of sixteen. Five is the
 *  same visible band the 8-cell tile had, at the new pitch. */
const DROP = 5;

/** Every wall in the building, casting.
 *
 *  ONE direction for the whole world — light from the north-west — so shadows agree with each
 *  other and with the offset every prop and person is drawn with. A per-room direction would be
 *  more correct and would read as noise.
 *
 *  Built from world.solid, which already knows every cell a body may not walk into — picks up
 *  structural mass and party walls between rooms for free, no second description of the same
 *  geometry.
 */
export function wallShadow(world: World): string {
  const solid = world.solid;
  const at = (x: number, y: number): boolean =>
    x >= 0 && y >= 0 && x < world.cols && y < world.rows && !!solid[y * world.cols + x];
  let d = "";
  for (let y = 0; y < world.rows; y++) {
    for (let x = 0; x < world.cols; x++) {
      if (at(x, y)) continue;
      const north = at(x, y - 1);
      const west = at(x - 1, y);
      const nw = at(x - 1, y - 1);
      const X = x * C;
      const Y = y * C;
      // Band under a wall runs the full tile; band beside one runs the full tile; where both
      // meet, the corner is already covered by the overlap.
      if (north) d += `M${X} ${Y}h${C}v${DROP}h-${C}z`;
      if (west) d += `M${X} ${Y + (north ? DROP : 0)}v${C - (north ? DROP : 0)}h${DROP}v-${C - (north ? DROP : 0)}z`;
      // An inside corner with nothing orthogonally solid still catches the diagonal.
      else if (nw && !north) d += `M${X} ${Y}h${DROP}v${DROP}h-${DROP}z`;
    }
  }
  return d;
}

/* The per-prop shadow PROJECTION system ends here, deliberately.
 *
 *  It existed because hand-drawn tiles carried no grounding of their own — six rounds of
 *  "floating trees" fought with projected silhouettes, dapple rules, contact invariants. Kenney
 *  art bakes grounding INTO the sprite (tree's bottom tile is trunk + skirt + contact shading;
 *  furniture carries feet and base shadows), and the reference register this view is held to —
 *  the packs' own sample scenes — draws NO thrown prop shadows at all. Re-projecting over art
 *  that already sits down would double-ground everything and read as collage. Wall band above
 *  stays: architecture, not a prop effect — keeps party walls legible as raised masonry.
 */
