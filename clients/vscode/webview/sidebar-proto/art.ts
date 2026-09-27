/** The SPINDLE's hardware: everything here, as rows of characters.
 *
 *  People, faces, status and sleep marks are NOT redrawn here — imported from workplace/art.ts
 *  through the workplace's own engine, so the same rectangles that draw a person in the building
 *  draw the same person on this board. Two surfaces stay coherent by sharing assets and a
 *  renderer, not by restyling one to resemble the other.
 *
 *  New here: the ironmongery a paper spike needs and a building doesn't — rod, point, base, and
 *  the curl an untouched slip takes on after hours.
 */
import type { Grid, Palette } from "../workplace/pixels";

export interface Piece {
  grid: Grid;
  pal: Palette;
}

/** Three tones left-to-right — highlight, body, shade — is the whole trick for reading a flat
 *  6px column as a cylinder. The rod's long middle is a CSS gradient with these same three stops,
 *  stretched to panel height — tip and base below must match these proportions exactly or the
 *  join shows.
 */
const ROD: Palette = {
  l: "var(--sp-rod-hi)",
  m: "var(--sp-rod)",
  d: "var(--sp-rod-lo)",
};

/** The point, poking up above the topmost slip. A spike with no point is a pipe. */
export const SPINDLE_TIP: Piece = {
  grid: [
    "..lm..",
    "..lm..",
    "..lm..",
    ".llmd.",
    ".llmd.",
    "llmmmd",
    "llmmmd",
  ],
  pal: ROD,
};

/** The weighted foot, under everything including the composer — one rod through every slip is
 *  what makes this panel ONE object, not two stacked ones. */
export const SPINDLE_BASE: Piece = {
  grid: [
    "..llmmmd..",
    "..llmmmd..",
    ".llmmmmdd.",
    "llmmmmmmdd",
    "dddddddddd",
    ".dddddddd.",
  ],
  pal: ROD,
};

/** The curl an untouched slip takes. Drawn, not written: a lifted corner says "nobody's been back
 *  in hours" before any timestamp is read — same reasoning the workplace uses to stop a stalled
 *  worker moving. */
export const DOG_EAR: Piece = {
  grid: [
    "ppppp.",
    "ppppq.",
    "pppqq.",
    "ppqqq.",
    "pqqqq.",
    ".qqqq.",
  ],
  pal: {
    p: "var(--sp-paper-shade)",
    q: "var(--sp-paper-back)",
  },
};

/** Pennant clipped to the slip of anyone who has left the building. The workplace says "outside"
 *  with a person past the exterior wall under a sky; a desk board has no outside, so it says the
 *  same fact with a flag instead. */
export const OUT_FLAG: Piece = {
  grid: [
    "pp....",
    "pFFFF.",
    "pFFFFF",
    "pFFFF.",
    "pp....",
    "pp....",
  ],
  pal: {
    p: "var(--sp-rod)",
    F: "var(--pod, var(--sp-rod-hi))",
  },
};

/** Staple holding a report's slip onto its lead's. Two legs and a bar, six pixels at scale 2 —
 *  the difference between "a nested list row" and "a smaller paper physically attached to a
 *  bigger one". */
export const STAPLE: Piece = {
  grid: [
    "mmmm",
    "m..m",
  ],
  pal: { m: "var(--sp-rod-lo)" },
};

/** What stands on the desk when the spike is empty.
 *
 *  The workplace answers "room is empty" with a furnished room, nobody in it — never a hole in
 *  the picture. Same answer here: stamp and pad sit there regardless, making the empty stretch a
 *  PLACE, not unlit canvas. Also: this is literally what stamps DONE and ERROR on the slips above.
 */
export const DESK_STAMP: Piece = {
  grid: [
    "...mmmm...........",
    "..mmmmmm..........",
    "...mmmm...........",
    "....nn............",
    "....nn............",
    "..bbbbbb..........",
    "..bbbbbb...ppppppp",
    "..bbbbbb...pIIIIIp",
    "..rrrrrr...ppppppp",
  ],
  pal: {
    m: "var(--sp-prop-hi)",
    n: "var(--sp-prop-lo)",
    b: "var(--sp-prop)",
    r: "var(--sp-prop-lo)",
    p: "var(--sp-prop)",
    I: "var(--sp-prop-lo)",
    // Named explicitly: the engine's default rim (workplace's literal ink) disappears against a
    // dark desk. A prop sitting ON the desk rims in the desk's own shadow line instead, so it
    // reads in both themes.
    "#": "var(--sp-desk-rim)",
  },
};

/** Corner of the pad, the way a blotter is actually held down: four leather mounts the sheet
 *  slides into. One grid placed four times and turned — matches the real object. Gives the empty
 *  stretch an EDGE, the difference between an empty room and a hole in the picture. */
export const PAD_CORNER: Piece = {
  grid: [
    "mmmmmmmm",
    "mmmmmmm.",
    "mmmmmm..",
    "mmmmm...",
    "mmmm....",
    "mmm.....",
    "mm......",
    "m.......",
  ],
  pal: { m: "var(--sp-prop)" },
};
