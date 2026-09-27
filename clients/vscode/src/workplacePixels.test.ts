/** The pixel engine emits SHAPES; what must never change is the PICTURE.
 *
 *  How a grid becomes SVG is an implementation detail and has already changed once — runs were an
 *  element each, now every run of one colour is a subpath of a single `<path>`, which is most of
 *  why the document went from 827 shapes to 62. Nothing about that is allowed to move a pixel, and
 *  a test that freezes the emitted string would have blocked the change rather than checked it.
 *
 *  So these read the markup BACK into painted cells and hold the engine to the two rules the art
 *  is authored against: a cell the grid names is painted at its own coordinate in the colour the
 *  palette gives it, and the only other thing painted is the derived rim. Any future emitter —
 *  vertical merging, a `<defs>` sheet — is checked against the art rather than against a snapshot.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { EMPTY, draw, drawFrames } from "../webview/workplace/pixels.ts";
import type { Grid, Palette } from "../webview/workplace/pixels.ts";
import {
  CLOUD, DISC, GHOST_PAL, HORIZON, MARKS, NOTE, POSE_MOVE, POSE_REST, PROPS,
  SKIN_PAL, SNOOZE, STAIRS,
} from "../webview/workplace/art.ts";

/** Painted cells, read back out of whatever shapes the engine chose to use. */
function painted(svg: string): Map<string, string> {
  const cells = new Map<string, string>();
  const put = (x: number, y: number, fill: string) => {
    const key = `${x},${y}`;
    assert.equal(cells.has(key), false, `cell ${key} painted twice — runs must not overlap`);
    cells.set(key, fill);
  };
  for (const m of svg.matchAll(/<rect x="(\d+)" y="(\d+)" width="(\d+)" height="1" fill="([^"]+)"\/>/g)) {
    for (let i = 0; i < +m[3]; i++) put(+m[1] + i, +m[2], m[4]);
  }
  for (const m of svg.matchAll(/<path fill="([^"]+)" d="([^"]+)"\/>/g)) {
    const fill = m[1];
    let consumed = 0;
    for (const r of m[2].matchAll(/M(\d+) (\d+)h(\d+)v1h-\3z/g)) {
      consumed += r[0].length;
      for (let i = 0; i < +r[3]; i++) put(+r[1] + i, +r[2], fill);
    }
    assert.equal(consumed, m[2].length, `unreadable path data: ${m[2].slice(0, 80)}`);
  }
  assert.ok(cells.size > 0, "nothing was painted at all");
  return cells;
}

/** The whole catalogue, so a new prop is covered the moment it is added to `PROPS`. */
const CATALOGUE: [string, Grid, Palette][] = [
  ...Object.entries(PROPS).map(([zone, p]) => [`prop:${zone}`, p.grid, p.pal] as [string, Grid, Palette]),
  ...Object.entries(MARKS).map(([s, p]) => [`mark:${s}`, p.grid, p.pal] as [string, Grid, Palette]),
  ["stairs", STAIRS.grid, STAIRS.pal],
  ["note", NOTE.grid, NOTE.pal],
  ["snooze", SNOOZE.grid, SNOOZE.pal],
  ["cloud", CLOUD.grid, CLOUD.pal],
  ["disc", DISC.grid, DISC.pal],
  ["horizon", HORIZON.grid, HORIZON.pal],
  ["person:rest", POSE_REST, SKIN_PAL],
  ["person:move", POSE_MOVE, SKIN_PAL],
  ["person:ghost", POSE_REST, GHOST_PAL],
];

for (const rim of [true, false]) {
  test(`every named cell is painted where the grid puts it, in the palette's colour (outline=${rim})`, () => {
    const pad = rim ? 1 : 0;
    for (const [what, grid, pal] of CATALOGUE) {
      const cells = painted(draw(grid, pal, { outline: rim }));
      for (let y = 0; y < grid.length; y++) {
        for (let x = 0; x < grid[y].length; x++) {
          const ch = grid[y][x];
          if (ch === EMPTY || !pal[ch]) continue;
          assert.equal(
            cells.get(`${x + pad},${y + pad}`),
            pal[ch],
            `${what}: cell ${x},${y} ('${ch}') lost its colour`,
          );
        }
      }
    }
  });
}

/** The picture is only half the contract: the whole reason a run becomes a subpath instead of its
 *  own element is that every run of one colour collapses into ONE `<path>`. A regression that
 *  paints correctly but stops grouping (one path per run again) is invisible to the pixel checks
 *  above — a wrong node count never moves a pixel — so it needs its own guard. */
test("every colour used becomes exactly one <path> — the whole point of merging runs by fill", () => {
  for (const [what, grid, pal] of CATALOGUE) {
    const svg = draw(grid, pal, { outline: true });
    const perFill = new Map<string, number>();
    for (const m of svg.matchAll(/<path fill="([^"]+)"/g)) perFill.set(m[1], (perFill.get(m[1]) ?? 0) + 1);
    assert.ok(perFill.size > 0, `${what}: nothing drawn`);
    for (const [fill, n] of perFill) {
      assert.equal(n, 1, `${what}: colour ${fill} split across ${n} <path> elements instead of merged into one`);
    }
  }
});

test("the derived rim is exactly the empty cells touching the art — no more, no less", () => {
  for (const [what, grid, pal] of CATALOGUE) {
    const cells = painted(draw(grid, pal, { outline: true }));
    const solid = (x: number, y: number) => {
      const row = grid[y];
      return row !== undefined && row[x] !== undefined && row[x] !== EMPTY;
    };
    const touches = (x: number, y: number) =>
      solid(x - 1, y) || solid(x + 1, y) || solid(x, y - 1) || solid(x, y + 1);
    const ink = pal["#"] ?? "var(--wp-ink)";

    // Nothing painted outside the art except rim, and rim only where it touches.
    for (const [key, fill] of cells) {
      const [x, y] = key.split(",").map((n) => Number(n) - 1); // back into grid space
      if (solid(x, y)) continue;
      assert.equal(fill, ink, `${what}: stray paint at ${x},${y}`);
      assert.ok(touches(x, y), `${what}: rim at ${x},${y} touches nothing — the silhouette has grown`);
    }
    // ...and the rim is actually THERE. Without this the silhouette can vanish silently: every
    // remaining cell would still be legitimate, because there would be none to object to.
    let rim = 0;
    for (let y = -1; y <= grid.length; y++) {
      for (let x = -1; x <= Math.max(...grid.map((r) => r.length)); x++) {
        if (solid(x, y) || !touches(x, y)) continue;
        rim++;
        assert.equal(cells.get(`${x + 1},${y + 1}`), ink, `${what}: no rim at ${x},${y}`);
      }
    }
    assert.ok(rim > 0, `${what}: nothing was outlined at all`);
  }
});

test("a rimless piece paints nothing the grid did not name", () => {
  for (const [what, grid, pal] of CATALOGUE) {
    const cells = painted(draw(grid, pal, { outline: false }));
    for (const key of cells.keys()) {
      const [x, y] = key.split(",").map(Number);
      const ch = grid[y]?.[x];
      assert.ok(ch !== undefined && ch !== EMPTY && pal[ch], `${what}: paint at ${x},${y} off the art`);
    }
  }
});

test("a two-frame sprite keeps both frames as the groups the stylesheet flips between", () => {
  const svg = drawFrames([POSE_REST, POSE_MOVE], SKIN_PAL, { className: "wp-sprite" });
  const groups = [...svg.matchAll(/<g class="wp-f wp-f(\d)">([\s\S]*?)<\/g>/g)];
  assert.equal(groups.length, 2, "both poses must be present in one svg or the sprite jitters");
  assert.equal(groups[0][1], "0");
  assert.equal(groups[1][1], "1");
  const [rest, move] = groups.map((g) => painted(g[2]));
  assert.notDeepEqual([...rest.keys()].sort(), [...move.keys()].sort(), "the frames are the same pose");
  // Both frames share one viewBox, which is the whole reason they live in one svg.
  assert.match(svg, /viewBox="0 0 12 18"/);
});

test("both frames of a sprite are drawn on the same canvas, whatever the scale asked for", () => {
  for (const scale of [2, 3, 4]) {
    const svg = drawFrames([POSE_REST, POSE_MOVE], SKIN_PAL, { scale });
    assert.match(svg, new RegExp(`width="${12 * scale}" height="${18 * scale}"`));
    assert.match(svg, /viewBox="0 0 12 18"/, "the art is scaled by the wrapper, never redrawn");
  }
});
