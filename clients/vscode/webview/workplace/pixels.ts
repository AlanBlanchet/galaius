/** The pixel engine: a char grid in, crisp SVG out.
 *
 *  Every piece of art in this view — a person, a server rack, a coffee machine — is authored as
 *  rows of characters and coloured by a palette. Nothing here knows what it is drawing, which is
 *  the point: the art is DATA (art.ts), this file is the only renderer, and a new prop costs a
 *  string literal rather than a hand-tuned SVG path.
 *
 *  Three things it does that authoring by hand would not:
 *   - the OUTLINE is derived, never drawn. Any empty cell touching a solid one becomes ink, so a
 *     sprite always reads against whatever wall it stands on and the art stays about the shape.
 *   - runs of one colour merge, and every run of the SAME colour is then emitted as ONE <path>,
 *     so a person costs 9 nodes instead of 69 and a wall of books 8 instead of 137 — one shape
 *     per colour for the browser to lay out, style and paint, most of why an empty workplace is
 *     62 shapes rather than 827.
 *   - a body already drawn is not drawn twice. Props don't change when a worker moves, and
 *     twelve running workers are twelve copies of one drawing, so the engine keeps what it has
 *     built and every render after the first is a lookup.
 */

/** Rows of equal length. "." is empty; every other char is a palette key. */
export type Grid = readonly string[];

/** Palette key to any CSS colour — a literal, or a var(--vscode-…) so it follows the theme. */
export type Palette = Readonly<Record<string, string>>;

export const EMPTY = ".";
/** Reserved: the derived outline writes into cells with this key. */
export const INK = "#";

export interface DrawOptions {
  /** Cell size in CSS pixels. Integers only — a fractional scale blurs the whole point. */
  scale?: number;
  /** Derive the ink rim. Off for flat props that are already framed. */
  outline?: boolean;
  /** Class on the <svg>. */
  className?: string;
  /** Extra attributes, already safe (we author them). */
  attrs?: string;
  /** Stretch to the container's width instead of an integer multiple. Only for art with no text
   *  and no square features — a distant horizon, never a sprite. */
  fluid?: boolean;
}

interface Cells {
  w: number;
  h: number;
  at: (x: number, y: number) => string;
  set: (x: number, y: number, ch: string) => void;
}

function cells(grid: Grid, pad: number): Cells {
  const w = Math.max(0, ...grid.map((r) => r.length)) + pad * 2;
  const h = grid.length + pad * 2;
  const buf: string[] = new Array(w * h).fill(EMPTY);
  grid.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) buf[(y + pad) * w + (x + pad)] = row[x];
  });
  return {
    w,
    h,
    at: (x, y) => (x < 0 || y < 0 || x >= w || y >= h ? EMPTY : buf[y * w + x]),
    set: (x, y, ch) => {
      if (x >= 0 && y >= 0 && x < w && y < h) buf[y * w + x] = ch;
    },
  };
}

/** An empty cell orthogonally touching a solid one is rim. Diagonals are deliberately left out —
 *  including them fattens every corner and the sprite loses its silhouette. */
function outline(c: Cells): void {
  const rim: [number, number][] = [];
  for (let y = 0; y < c.h; y++) {
    for (let x = 0; x < c.w; x++) {
      if (c.at(x, y) !== EMPTY) continue;
      const touching =
        c.at(x - 1, y) !== EMPTY ||
        c.at(x + 1, y) !== EMPTY ||
        c.at(x, y - 1) !== EMPTY ||
        c.at(x, y + 1) !== EMPTY;
      if (touching) rim.push([x, y]);
    }
  }
  for (const [x, y] of rim) c.set(x, y, INK);
}

/** Horizontal run-length merge, then one <path> per colour.
 *
 *  Runs are still exactly the runs the art was authored as — a wrong pixel is found by reading
 *  along the row it's on, why vertical merging is still refused. What changed is what carries
 *  them: a run is an "M x y h w v1 h-w z" subpath rather than an element of its own, so all the
 *  metal in a server rack is ONE node instead of forty, colour named once instead of forty times.
 *  Runs never overlap, so painting them grouped by colour puts the same cells on screen as
 *  painting them in scan order. */
function paint(c: Cells, pal: Palette): string {
  const byFill = new Map<string, string[]>();
  for (let y = 0; y < c.h; y++) {
    let x = 0;
    while (x < c.w) {
      const ch = c.at(x, y);
      let run = 1;
      while (x + run < c.w && c.at(x + run, y) === ch) run++;
      // The rim is derived, so its colour is too — a palette that never mentions ink still gets a
      // silhouette, and a piece that wants a different rim just declares one.
      const fill = ch === EMPTY ? null : (pal[ch] ?? (ch === INK ? "var(--wp-ink)" : null));
      if (fill) {
        let d = byFill.get(fill);
        if (!d) byFill.set(fill, (d = []));
        d.push(`M${x} ${y}h${run}v1h-${run}z`);
      }
      x += run;
    }
  }
  let out = "";
  for (const [fill, d] of byFill) out += `<path fill="${fill}" d="${d.join("")}"/>`;
  return out;
}

/** A drawn body and the grid it fills: everything about a piece of art that does not depend on
 *  how big it is asked to be. */
interface Body {
  svg: string;
  w: number;
  h: number;
}

/** Identity, so a grid and a palette can key a cache without being stringified. Both are module
 *  constants in art.ts, so the numbering is bounded by how many pieces of art exist. */
const ids = new WeakMap<object, number>();
let nextId = 0;
function idOf(o: object): number {
  let id = ids.get(o);
  if (id === undefined) ids.set(o, (id = nextId++));
  return id;
}

/** What a piece of art actually costs: pad, derive the rim, merge the runs, name the colours.
 *  Pure in its three arguments, and the whole view draws from about two dozen combinations of
 *  them, so it's computed once per combination and never again. Deliberately keyed on the art
 *  alone: scale, class and attributes belong to the <svg> wrapper, a hundred characters of
 *  concatenation built fresh every time. */
const bodies = new Map<string, Body>();
function bodyOf(grid: Grid, pal: Palette, rim: boolean): Body {
  const key = `${idOf(grid)}:${idOf(pal)}:${rim ? 1 : 0}`;
  let body = bodies.get(key);
  if (!body) {
    const c = cells(grid, rim ? 1 : 0);
    if (rim) outline(c);
    bodies.set(key, (body = { svg: paint(c, pal), w: c.w, h: c.h }));
  }
  return body;
}

function open(w: number, h: number, size: string, opts: DrawOptions): string {
  const { className = "", attrs = "" } = opts;
  return (
    `<svg${className ? ` class="${className}"` : ""} ${size} viewBox="0 0 ${w} ${h}" ` +
    `shape-rendering="crispEdges" aria-hidden="true" focusable="false"${attrs ? " " + attrs : ""}>`
  );
}

/* ── one drawing, referenced many times ──────────────────────────────
 *
 *  The engine already refuses to BUILD a body twice, but still WROTE it out at every call site —
 *  and a workplace is overwhelmingly the same few drawings repeated: one worker sprite per
 *  person, one prop per room, the same marks and stamps everywhere. At fifteen people that was
 *  wasteful; at a hundred and fifty the document reached 621 kB, and with the live loop that's
 *  now 621 kB pushed down the message channel on every refresh, not just a rebuild.
 *
 *  So a render may open a SHEET: every distinct body is written once into a <defs> block, each
 *  call site becomes a <use>. Two details make it safe rather than clever:
 *
 *   - ids live in a <g>, never a <symbol>. A <symbol> establishes its own viewport and would
 *     re-scale content whose frames differ in size; a <g> places the identical paths at the
 *     identical coordinates, so the pixels are the ones already reviewed.
 *   - drawFrames puts its wp-fN class on the <use> ELEMENT, not inside the referenced body.
 *     Content inside a use is a shadow tree the stylesheet cannot select, so a class buried in
 *     there would silently kill every frame animation in the building.
 *
 *  With no sheet open, both functions inline exactly as before — a caller rendering one sprite
 *  on its own (the side bar borrows this engine) needs to know nothing about any of it.
 */
interface Sheet {
  ids: Map<string, string>;
  parts: string[];
}
let sheet: Sheet | null = null;

/** Begin collecting bodies. Every draw until closeSheet emits a reference instead of a copy. */
export function openSheet(): void {
  sheet = { ids: new Map(), parts: [] };
}

/** The <defs> block for everything drawn since openSheet, and the end of collecting. Placement
 *  in the document doesn't matter — before or after the references, id resolution doesn't care —
 *  but callers put it at the top of the scene where it can't be dropped. */
export function closeSheet(): string {
  const open_ = sheet;
  sheet = null;
  if (!open_ || !open_.parts.length) return "";
  return (
    `<svg class="wp-defs" width="0" height="0" aria-hidden="true" focusable="false">` +
    `<defs>${open_.parts.join("")}</defs></svg>`
  );
}

/** The body's markup, or a reference to it when a sheet is collecting. */
function useOrInline(grid: Grid, pal: Palette, rim: boolean, body: Body): string {
  if (!sheet) return body.svg;
  const key = `${idOf(grid)}:${idOf(pal)}:${rim ? 1 : 0}`;
  let id = sheet.ids.get(key);
  if (!id) {
    id = `wp-b${sheet.ids.size}`;
    sheet.ids.set(key, id);
    sheet.parts.push(`<g id="${id}">${body.svg}</g>`);
  }
  return `<use href="#${id}"/>`;
}

/** One grid as a standalone <svg>, sized to an exact integer multiple so edges stay hard. */
export function draw(grid: Grid, pal: Palette, opts: DrawOptions = {}): string {
  const { scale = 2, outline: rim = true, fluid = false } = opts;
  const body = bodyOf(grid, pal, rim);
  const size = fluid
    ? `width="100%" height="${body.h * scale}" preserveAspectRatio="none"`
    : `width="${body.w * scale}" height="${body.h * scale}"`;
  return `${open(body.w, body.h, size, opts)}${useOrInline(grid, pal, rim, body)}</svg>`;
}

/** One grid PLACED at a coordinate inside an enclosing SVG, as one node instead of two.
 *
 *  draw has to wrap its body in an <svg> because a caller may put it anywhere in an HTML
 *  document, at any scale, needing a box. Inside the map that box is redundant: the map's own
 *  user units ARE tile cells, a tile authored 8x8 and drawn at scale 1, so the wrapper is
 *  exactly <svg x y width="8" height="8" viewBox="0 0 8 8"> — what a bare <use x y> already
 *  means. The wrapper was one element per prop and one per prop SHADOW, for no geometry at all.
 *
 *  Measured on the landscaped site: four hundred and thirty-six plants plus their silhouettes is
 *  about nine hundred wrapper nodes the browser lays out, styles and paints for nothing. Falls
 *  back to draw with no sheet open, so a caller outside a scene render is unaffected.
 */
export function place(grid: Grid, pal: Palette, x: number, y: number, opts: DrawOptions = {}): string {
  const { outline: rim = false, className = "", attrs = "" } = opts;
  if (!sheet) return draw(grid, pal, { ...opts, attrs: `x="${x}" y="${y}"` + (attrs ? " " + attrs : "") });
  const ref = useOrInline(grid, pal, rim, bodyOf(grid, pal, rim));
  return ref.replace(
    "<use ",
    `<use ${className ? `class="${className}" ` : ""}x="${x}" y="${y}"${attrs ? " " + attrs : ""} `,
  );
}

/** Two poses in ONE <svg>, stacked as groups the stylesheet flips between. A frame animation
 *  needs both frames present and identically placed; drawing them as separate elements is how
 *  the sprite ends up jittering by a pixel when one grid is a row taller than the other. */
export function drawFrames(
  frames: readonly Grid[],
  pal: Palette,
  opts: DrawOptions = {},
): string {
  const { scale = 2, outline: rim = true } = opts;
  const built = frames.map((g) => bodyOf(g, pal, rim));
  const w = Math.max(...built.map((b) => b.w));
  const h = Math.max(...built.map((b) => b.h));
  // Class stays on the <use> ELEMENT, never inside the referenced body (shadow tree, unselectable
  // by the stylesheet — see the sheet note above for why).
  const groups = built
    .map((b, i) => {
      const ref = useOrInline(frames[i], pal, rim, b);
      return ref.startsWith("<use")
        ? ref.replace("<use ", `<use class="wp-f wp-f${i}" `)
        : `<g class="wp-f wp-f${i}">${ref}</g>`;
    })
    .join("");
  return `${open(w, h, `width="${w * scale}" height="${h * scale}"`, opts)}${groups}</svg>`;
}

/** Rendered size of a grid, so layout can reserve room without measuring in the browser. */
export function sizeOf(grid: Grid, scale: number, rim = true): [number, number] {
  const pad = rim ? 2 : 0;
  return [(Math.max(0, ...grid.map((r) => r.length)) + pad) * scale, (grid.length + pad) * scale];
}
