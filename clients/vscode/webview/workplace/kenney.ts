/** The bridge from the Kenney atlas to the document: defs, tile placement, and the paper doll.
 *
 *  The atlas is ONE <image> in the document's defs; every 16x16 cell of it is exposed as a
 *  nested <svg id="kc-N" viewBox> crop, and every TILE is a <g id="wt-id"> of cell uses. A call
 *  site then costs one <use>, exactly like the pixel sheet before it — the document grows with
 *  references, never with pixels.
 *
 *  The PEOPLE are compositions, not fixed sprites: the Roguelike Characters pack is a paper
 *  doll — bare bodies, shirts, hair, beards, hats as separate cells — so a worker is body +
 *  shirt + hair layered in one 16x16 frame. The shirt is the POD's (the team hue picks the
 *  nearest Kenney garment), the face is the RUN's (skin, hair style, hair colour, beard from its
 *  id) — the same two-axis identity rule the drawn sprites carried, on professional art.
 */
import { ATLAS_COLS, ATLAS_ROWS, ATLAS_URI, CELL, K } from "./atlas";
import type { CellName } from "./atlas";
import { TILES, TILE_CELLS } from "./tiles";
import type { TileId } from "./tiles";
import { hash } from "./palette";

/** Every cell crop plus every tile group, once, at the top of the document.
 *
 *  All cells are exposed rather than only the ones the map happens to place, because the DOLLS
 *  are composed at runtime from hashes — which cells a given roster wants is not knowable when
 *  the defs are written, and the whole block is a few kilobytes. */
export function atlasDefs(): string {
  let out =
    `<svg class="wp-defs" width="0" height="0" aria-hidden="true" focusable="false"><defs>` +
    `<image id="wp-atlas" href="${ATLAS_URI}" width="${ATLAS_COLS * CELL}" ` +
    `height="${ATLAS_ROWS * CELL}" style="image-rendering:pixelated"/>`;
  for (const [name, i] of Object.entries(K)) {
    const cx = (i % ATLAS_COLS) * CELL;
    const cy = Math.floor(i / ATLAS_COLS) * CELL;
    out +=
      `<svg id="kc-${name}" viewBox="${cx} ${cy} ${CELL} ${CELL}" width="${CELL}" ` +
      `height="${CELL}"><use href="#wp-atlas"/></svg>`;
  }
  for (const [id, tile] of Object.entries(TILES)) {
    out += `<g id="wt-${id}">`;
    for (const c of tile.cells) {
      out += `<use href="#kc-${c.n}" x="${c.dx * TILE_CELLS}" y="${c.dy * TILE_CELLS}"/>`;
    }
    out += `</g>`;
  }
  return out + `</defs></svg>`;
}

/** One tile at a cell of the map, in the map's own user units (cells). */
export function tileUse(id: TileId, x: number, y: number, cls = "", extra = ""): string {
  return (
    `<use href="#wt-${id}" x="${x * TILE_CELLS}" y="${y * TILE_CELLS}"` +
    (cls ? ` class="${cls}"` : "") +
    (extra ? ` ${extra}` : "") +
    `/>`
  );
}

/** One raw atlas cell, for pattern contents and compositions. */
export function cellUse(n: CellName, x = 0, y = 0): string {
  return `<use href="#kc-${n}" x="${x}" y="${y}"/>`;
}

/* ── the paper doll ── */

const BODIES: CellName[] = ["body0", "body1", "body2"];
/** The pod's garment per accent hue (1..7 — the --wp-h* the rail and the ring use). Kenney
 *  wardrobe has no exact chart colours, so each hue takes the nearest DISTINCT garment; what
 *  must hold: two pods never dress alike, and the ring, minimap dot and nameplate underline
 *  still carry the exact accent. */
const SHIRTS: CellName[] = ["shirt1", "shirt2", "shirt3", "shirt4", "shirt5", "shirt6", "shirt7"];
const HAIR_BLOCKS = 5;
const HAIR_STYLES = 5;

export interface Doll {
  body: CellName;
  shirt: CellName;
  hair: CellName | null;
  beard: CellName | null;
  hat: CellName | null;
}

export function dollOf(runId: string, hue: number, opts: { foreign?: boolean; brain?: boolean } = {}): Doll {
  const h = hash(runId);
  const body = BODIES[h % BODIES.length];
  const shirt: CellName = opts.foreign
    ? "shirtDark"
    : SHIRTS[Math.max(0, Math.min(SHIRTS.length - 1, hue - 1))];
  const block = (h >>> 3) % HAIR_BLOCKS;
  const style = (h >>> 7) % HAIR_STYLES;
  const hair = `hair${block}${style}` as CellName;
  const beard = (h >>> 13) % 4 === 0 ? (`beard${block}` as CellName) : null;
  return { body, shirt, hair, beard, hat: opts.brain ? "hatBrain" : null };
}

/** The composed character, one svg. Layer order is the paper doll's own: body, shirt, beard,
 *  hair, hat. Scale 2 — a 16px person on a 32px tile, the proportion the urban pack's sample
 *  town uses. */
export function dollSvg(doll: Doll, cls = ""): string {
  let layers = cellUse(doll.body) + cellUse(doll.shirt);
  if (doll.beard) layers += cellUse(doll.beard);
  if (doll.hair) layers += cellUse(doll.hair);
  if (doll.hat) layers += cellUse(doll.hat);
  return (
    `<svg class="wp-doll${cls ? " " + cls : ""}" viewBox="0 0 ${CELL} ${CELL}" ` +
    `width="${CELL * 2}" height="${CELL * 2}" aria-hidden="true" focusable="false">` +
    layers +
    `</svg>`
  );
}
