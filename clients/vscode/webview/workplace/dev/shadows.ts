/** THE ART CONTRACT, checked against the composed tiles and the rendered document.
 *
 *  This probe used to guard the projected-shadow system — six rounds of "floating trees" were
 *  fought here, and the file's history is the argument for its existence: every one of those
 *  defects was invisible to a unit test and camouflaged in a screenshot. The projection died
 *  with the hand-drawn art (the Kenney sprites carry their own grounding), but the CLASS of
 *  defect it guarded did not: art that detaches from the cell it claims to stand on, and a
 *  document whose layers paint in an order nobody decided. So the invariants moved up a level:
 *
 *   1. A TILE STANDS ON ITS ANCHOR. Every composition includes the anchor cell, nothing hangs
 *      below it, and a tile that blocks N ground cells draws on all N — solid on paper and
 *      invisible in pixels is how a body walks through a couch.
 *   2. EVERY CELL A TILE NAMES EXISTS IN THE ATLAS, and every reference the rendered document
 *      makes (#wt-*, #kc-*) has a definition — a <use> with no target renders NOTHING,
 *      silently, which is the exact shape of the old floating-tree bugs.
 *   3. THE LAYERS PAINT IN THE DECIDED ORDER: grounds, then the wall band, then everything that
 *      stands up — and no remnant of the dead shadow layer survives in the markup.
 *   4. EVERY SEAT IS REACHABLE FROM ITS OWN DOOR. The clearance net exists because five locally
 *      legal emitters composed into two sealed departments on a real roster; this re-runs the
 *      check the net promises, over the dev fixture's world.
 *
 *  Prints per section, exits non-zero on any violation:
 *
 *      node out-shadows.js
 */
import { ATLAS_COLS, ATLAS_ROWS, ATLAS_URI, K } from "../atlas";
import { TILES, footprint } from "../tiles";
import type { TileId } from "../tiles";
import { renderScene, worldFor } from "../scene";
import type { Cast } from "../scene";
import { fixture } from "./fixture";

let bad = 0;
function fail(msg: string): void {
  bad++;
  console.log("FAIL  " + msg);
}

/* ── 1. composition ── */
{
  let checked = 0;
  for (const [id, tile] of Object.entries(TILES)) {
    checked++;
    if (!tile.cells.length) fail(`${id}: no cells at all`);
    if (!(tile.cells as { n: string }[]).every((c) => c.n in K)) {
      fail(`${id}: names a cell the atlas does not carry`);
    }
    const dys = tile.cells.map((c) => c.dy);
    if (Math.max(...dys) > 0) fail(`${id}: art hangs BELOW its anchor cell`);
    if (Math.max(...dys) < 0) fail(`${id}: nothing stands on the anchor cell`);
    if (tile.solid) {
      const ground = new Set(tile.cells.filter((c) => c.dy === 0).map((c) => c.dx));
      for (const [dx] of footprint(id as TileId)) {
        if (!ground.has(dx)) fail(`${id}: blocks (${dx},0) but draws nothing there`);
      }
    }
  }
  console.log(`tiles: ${checked} compositions, anchors and footprints agree`);
}

/* ── 2. the atlas itself, and every reference the document makes ── */
const scene = renderScene(fixture());
{
  const names = Object.keys(K).length;
  if (ATLAS_COLS * ATLAS_ROWS < names) fail(`atlas grid ${ATLAS_COLS}x${ATLAS_ROWS} < ${names} names`);
  if (!/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(ATLAS_URI)) fail("atlas URI is not a png data uri");

  const defined = new Set<string>();
  for (const m of scene.matchAll(/<(?:g|svg) id="((?:wt|kc)-[^"]+)"/g)) defined.add(m[1]);
  const used = new Set<string>();
  for (const m of scene.matchAll(/href="#((?:wt|kc)-[^"]+)"/g)) used.add(m[1]);
  let missing = 0;
  for (const ref of used) {
    if (!defined.has(ref)) {
      missing++;
      fail(`document references #${ref} with no definition`);
    }
  }
  console.log(`defs: ${defined.size} defined, ${used.size} referenced, ${missing} dangling`);
}

/* ── 3. paint order, and no remnant of the dead layer ── */
{
  const map = scene.indexOf('<svg class="wp-map"');
  const ao = scene.indexOf('class="wp-ao"', map);
  const firstRoomStructure = scene.indexOf('class="wp-rm wp-bu', map);
  if (ao < 0) fail("the wall band (wp-ao) is gone from the map");
  if (firstRoomStructure < 0) fail("no structure groups in the map");
  if (ao > firstRoomStructure) fail("the wall band paints OVER the rooms' structure");
  for (const ghost of ["wp-shadow", "wp-drop", "wp-struct"]) {
    if (scene.includes(ghost)) fail(`the dead layer survives in the markup: ${ghost}`);
  }
  console.log("layers: grounds, wall band, structure — in that order, no remnants");
}

/* ── 4. every seat reachable from its own door ── */
{
  const world = worldFor(fixture().workers as Cast[]);
  let rooms = 0;
  let seats = 0;
  for (const room of world.rooms) {
    if (room.open || room.outdoor || !room.doors.length) continue;
    rooms++;
    const inRoom = (x: number, y: number): boolean =>
      room.rects.some((r) => x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h);
    const seen = new Set<number>();
    const q: number[] = [];
    for (const d of room.doors) {
      seen.add(d.y * world.cols + d.x);
      q.push(d.y * world.cols + d.x);
    }
    for (let head = 0; head < q.length; head++) {
      const cur = q[head];
      const x = cur % world.cols;
      const y = (cur - x) / world.cols;
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const nx = x + dx;
        const ny = y + dy;
        if (!inRoom(nx, ny)) continue;
        const k = ny * world.cols + nx;
        if (world.solid[k] || seen.has(k)) continue;
        seen.add(k);
        q.push(k);
      }
    }
    for (const s of room.seats) {
      seats++;
      if (!seen.has(s.y * world.cols + s.x)) {
        fail(`${room.id}: seat (${s.x},${s.y}) is sealed off from its own door`);
      }
    }
  }
  console.log(`clearance: ${seats} seats across ${rooms} walled rooms, all reachable from a door`);
}

if (bad) {
  console.log(`\n${bad} violation(s)`);
  process.exit(1);
}
console.log("\nOK");
