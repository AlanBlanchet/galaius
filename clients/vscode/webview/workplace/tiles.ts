/** The SUBSTRATE: professional tiles, composed from the Kenney atlas.
 *
 *  The hand-drawn 8x8 tiles hit their ceiling — two full craft passes and the verdict stayed
 *  "nothing is usable professionally". Real products ship real assets, so every tile here is now
 *  a COMPOSITION of 16x16 cells from three CC0 Kenney packs (media/world/CREDITS.md): the RPG
 *  Urban Pack for the site and the walls, Roguelike Indoors for the furniture, Roguelike
 *  Characters for the people. This file owns WHICH cells make a desk, a tree or a piano; the
 *  atlas owns the pixels; nothing in the plan (world.ts) changed its vocabulary.
 *
 *  A tile is anchored at its walkability cell. Cells with dy < 0 rise ABOVE the anchor (a
 *  tree's crown, a wardrobe's upper half) and never block movement; foot is how many ground
 *  cells wide the thing stands (a two-seat couch is two). The map's user units are CELLS —
 *  sixteen per tile — and a tile is drawn at 32 CSS pixels, so one source pixel is two device
 *  pixels at zoom 1 and the camera's half-step ladder keeps every rung pixel-exact.
 */
import { K } from "./atlas";
import type { CellName } from "./atlas";

/** One atlas cell placed relative to the tile's anchor, in TILE units. */
export interface Placement {
  n: CellName;
  dx: number;
  dy: number;
}

export interface Tile {
  cells: Placement[];
  /** Solid tiles block movement — the anchor cell plus foot - 1 cells to its east. */
  solid?: boolean;
  /** Ground cells wide. Only >1 for genuinely wide furniture (a couch, a piano, a parked car). */
  foot?: number;
}

const one = (n: CellName, solid = true): Tile => ({ cells: [{ n, dx: 0, dy: 0 }], solid });
const flat = (n: CellName): Tile => ({ cells: [{ n, dx: 0, dy: 0 }] });
const tall = (top: CellName, bottom: CellName, solid = true): Tile => ({
  cells: [{ n: top, dx: 0, dy: -1 }, { n: bottom, dx: 0, dy: 0 }],
  solid,
});
const wide = (l: CellName, r: CellName, solid = true): Tile => ({
  cells: [{ n: l, dx: 0, dy: 0 }, { n: r, dx: 1, dy: 0 }],
  solid,
  foot: 2,
});
const big = (tl: CellName, tr: CellName, bl: CellName, br: CellName): Tile => ({
  cells: [
    { n: tl, dx: 0, dy: -1 }, { n: tr, dx: 1, dy: -1 },
    { n: bl, dx: 0, dy: 0 }, { n: br, dx: 1, dy: 0 },
  ],
  solid: true,
  foot: 2,
});

export const TILES = {
  /* ── ground (never solid, drawn as pattern runs) ── */
  floor: flat("plazaGrey"),
  carpet: flat("plazaTan"),
  dais: flat("plazaTan"),
  lino: flat("plateGrey"),
  runner: flat("plazaTan"),
  rug: flat("kerbTan"),
  mass: flat("roofRed"),
  grass: flat("grass"),
  meadow: flat("meadow"),
  litter: flat("meadow"),
  earth: flat("plazaTan"),
  path: flat("plateGrey"),
  pond: flat("water"),
  road: flat("road"),
  roadDash: flat("roadDash"),
  roadCross: flat("roadCross"),
  roadP: flat("roadP"),

  /* ── walls & doors ── */
  wall: { cells: [{ n: "roofO", dx: 0, dy: 0 }], solid: true } as Tile,
  face: { cells: [{ n: "roofOEdge", dx: 0, dy: 0 }], solid: true } as Tile,
  window: one("winTall"),
  doorH: one("doorWhite", false),
  doorV: one("doorWhite", false),
  doorCap: flat("roofO"),
  doorWay: flat("plateGrey"),
  doorLeaf: one("doorWhite", false),
  matt: flat("kerbTan"),

  /* ── furniture ── */
  desk: one("counterPaper"),
  drafting: one("counterPlain"),
  shelf: one("dresser"),
  stacks: tall("tallBrownT", "tallBrownB"),
  cabinet: tall("tallGreenT", "tallGreenB"),
  rack: tall("speakerA", "speakerB"),
  plant: one("plantA"),
  sofa: wide("sofaL", "sofaR"),
  sofaG: wide("sofaGL", "sofaGR"),
  armchair: one("armchair"),
  bench: one("benchWood"),
  urn: one("binGrey"),
  core: big("organTL", "organTR", "organBL", "organBR"),
  piano: big("pianoTL", "pianoTR", "pianoBL", "pianoBR"),
  board: one("boardGreen"),
  screen: one("panelCream"),
  tableL: one("table3L"),
  tableM: one("tableRound"),
  tableR: one("table3R"),
  chair: one("chairS"),
  stool: one("stool"),
  coffee: one("counterKettle"),
  counter: one("counterPlain"),
  sink: one("counterSink"),
  cooler: one("fridge"),
  printer: one("washer"),
  crates: one("crateFull"),
  tank: one("aquaL"),
  vending: tall("vendT", "vendB"),
  stove: tall("stoveT", "stoveB"),
  globe: one("shelfPotions"),
  dish: one("lightTall"),
  ladder: one("ladder"),
  pillar: one("candStand"),
  mirror: tall("mirrorT", "mirrorB"),
  ward: big("wardTL", "wardTR", "wardBL", "wardBR"),

  /* ── on the wall face (flat: the face row is already solid masonry) ── */
  winFace: flat("winArch"),
  whiteboard: flat("boardBeige"),
  clock: flat("fOrange"),
  pinboard: flat("bulletin"),
  poster: flat("fGreen"),
  pipes: flat("organTR"),
  vent: flat("machineGrey"),
  screenWall: flat("aquaM"),
  lamp: flat("sconce"),

  /* ── outdoors ── */
  tree: tall("treeAC", "treeAT"),
  treeBig: tall("autumnC", "autumnT"),
  pine: tall("treeBC", "treeBT"),
  bush: one("bushSq"),
  rock: one("stoneA"),
  blooms: one("shrub"),
  tuft: one("treeTiny"),
  planter: one("planterBush"),
  lamppost: one("lamppost"),
  mast: one("lightTall"),
  benchPark: one("benchPark"),
  hydrant: one("hydrant"),
  mailbox: one("mailbox"),
  barrel: one("barrel"),
  stall: one("stallStripe"),
  taxi: wide("taxiL", "taxiR"),
  carRed: wide("carRedL", "carRedR"),
  van: tall("vanGreenT", "vanGreenB"),
} as const satisfies Record<string, Tile>;

export type TileId = keyof typeof TILES;

/** The ground cells a solid tile occupies, in tile units off its anchor. */
export function footprint(id: TileId): [number, number][] {
  const t: Tile = TILES[id];
  if (!t.solid) return [];
  const foot = t.foot ?? 1;
  const out: [number, number][] = [];
  for (let i = 0; i < foot; i++) out.push([i, 0]);
  return out;
}

/** How big a tile is drawn, and the sprite scale that matches it. Stated once here because the
 *  stylesheet, the renderer and the simulation all have to agree on it to the pixel — a body's
 *  position is in tiles and its element is placed in CSS pixels. Sixteen source pixels drawn at
 *  32 makes one source pixel exactly TWO device pixels at zoom 1, which is what lets the camera
 *  ladder run in halves and stay crisp at every rung. */
export const TILE_PX = 32;
export const TILE_CELLS = 16;
export const TILE_SCALE = TILE_PX / TILE_CELLS;
