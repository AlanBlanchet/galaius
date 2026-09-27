/** WCAG contrast over the workplace's own colour algebra.
 *
 *  The workplace paints text on translucent plates that sit over room art, so a plate's
 *  effective contrast is a COMPOSITE — it depends on what's behind it and on any opacity applied
 *  to an ancestor. Two contrast defects shipped on this surface because that stack was judged by
 *  eye (and once by a computed-style read, which can't see an ancestor's opacity at all). This
 *  module makes the stack computable so a test can hold the floor.
 */

export type Rgb = readonly [number, number, number];

const clamp = (n: number): number => (n < 0 ? 0 : n > 255 ? 255 : n);

/** color-mix(in srgb, a <aPct>%, b) — sRGB, the space the stylesheet mixes in. */
export function mix(a: Rgb, b: Rgb, aPct: number): Rgb {
  const w = aPct / 100;
  return [0, 1, 2].map((i) => clamp(a[i] * w + b[i] * (1 - w))) as unknown as Rgb;
}

/** Source-over compositing: fg at alpha painted on opaque bg. */
export function over(fg: Rgb, bg: Rgb, alpha: number): Rgb {
  return mix(fg, bg, alpha * 100);
}

/** #rgb, #rgba, #rrggbb and #rrggbbaa. Alpha is parsed and dropped — these colours are
 *  composited explicitly by over, so silently treating "#fff8" as "#fff" (and its alpha as part
 *  of the red channel) would have quietly corrupted every ratio computed from it. */
export function parseHex(hex: string): Rgb {
  const h = hex.replace("#", "").trim();
  const short = h.length === 3 || h.length === 4;
  if (![3, 4, 6, 8].includes(h.length)) throw new Error(`not a hex colour: ${hex}`);
  const full = short ? h.split("").map((c) => c + c).join("") : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as unknown as Rgb;
}

function channel(v: number): number {
  const s = v / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

export function relativeLuminance(c: Rgb): number {
  return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]);
}

export function contrastRatio(a: Rgb, b: Rgb): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** WCAG AA for body-size text. The workplace's plates are small, so this is the floor, not the goal. */
export const AA = 4.5;
