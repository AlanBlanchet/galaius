/** Who looks like whom.
 *
 *  Two jobs pulling opposite ways: a worker must be an INDIVIDUAL ("the one with the dark green
 *  hair is back", no label needed), and a pod must be a TEAM — a report three rooms away still
 *  has to read as its lead's.
 *
 *  So the axes split: shirt is the POD's colour (from the lead's id, worn by everyone under
 *  them — survives the distance), skin/hair/trousers are the WORKER's own, from their run id.
 *  Same lead, same shirt; same person, same face, every refresh — nothing random.
 */

/** FNV-1a. A hash, not a random: the point is that a run keeps its face between snapshots. */
export function hash(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

function pick<T>(list: readonly T[], seed: number, salt: number): T {
  return list[(seed >>> salt) % list.length];
}

/** Skin and hair stay literal, not theme-following — tinted by the editor background, a person
 *  stops being a person. Tones chosen to hold against a light AND dark wall; that's what the
 *  derived ink rim is for. */
const SKIN = ["#f0c39b", "#e0a877", "#c68642", "#a8663c", "#7d4b2a", "#f7d9bd"] as const;
const HAIR = ["#2b2118", "#553311", "#8a5a2b", "#c9a227", "#7a2f2f", "#3b3f4a", "#b8b0a6"] as const;
const TROUSER = ["#3b4256", "#4a3f52", "#2f4a42", "#54463a", "#3f3f3f"] as const;
const BOOT = ["#2a2420", "#3a2f28", "#221d2b"] as const;

/** Seven hues from the theme's chart palette (plus two mixed in) so a light theme doesn't get
 *  neon. None is RED: red already means "stuck" elsewhere on this surface — a red team would
 *  read as a team on fire. */
export const HUES = 7;

/** Colours for every pod on screen at once, guaranteed DISTINCT.
 *
 *  Plain hash % HUES was the obvious version and wrong: five leads, five hues, a collision is
 *  more likely than not — and a collision silently merges two teams, since pod colour is the
 *  only thing tying a distant report back to its lead.
 *
 *  So hash picks a PREFERENCE and assignment resolves clashes: leads walked in stable order,
 *  each takes its preferred hue, a taken preference steps to the next free one. Distinct up to
 *  seven leads; hash over plain index so a lead keeps its colour as others join or leave.
 */
export function assignAccents(leadRunIds: readonly string[]): Map<string, string> {
  const taken = new Set<number>();
  const out = new Map<string, string>();
  for (const id of [...leadRunIds].sort()) {
    let hue = hash(id) % HUES;
    for (let step = 0; step < HUES && taken.has(hue); step++) hue = (hue + 1) % HUES;
    taken.add(hue);
    out.set(id, `var(--wp-h${hue + 1})`);
  }
  return out;
}

/** The four properties that make this worker this worker. Shirt is deliberately absent — it
 *  comes from the pod, through the cascade. */
export function faceOf(runId: string): string {
  const h = hash(runId);
  return (
    `--c-skin:${pick(SKIN, h, 0)};` +
    `--c-hair:${pick(HAIR, h, 5)};` +
    `--c-trouser:${pick(TROUSER, h, 11)};` +
    `--c-boot:${pick(BOOT, h, 17)}`
  );
}

/** How stopped they are, 0 to 1 — the number the fade/slump/dust treatment hangs off. Ten
 *  minutes is the ceiling: past it, it's not "how long" any more, it's "not coming back on its
 *  own". */
export function idleAmount(seconds: number): number {
  const t = Math.max(0, seconds) / 600;
  return Math.round(Math.min(1, t) * 100) / 100;
}

/** Epoch milliseconds, from a stamp that may be in either unit.
 *
 *  Data layer works in SECONDS (Python's time.time()), JS date constructors want milliseconds —
 *  a stamp handed straight to new Date() renders 1970. Dev fixtures used Date.UTC(...), already
 *  milliseconds, so the harness looked right while the product showed 1970: normalise at the
 *  boundary, never trust the caller.
 *
 *  Below the threshold can't be a plausible millisecond time (1970); above it can't be a
 *  plausible second time (year 5138). */
export function atMillis(stamp: number): number {
  return stamp < 1e11 ? stamp * 1000 : stamp;
}

/** The same stamp as epoch SECONDS, for comparing two of them. */
export function atSeconds(stamp: number): number {
  return stamp < 1e11 ? stamp : stamp / 1000;
}

/** "4m", "2h" — said the way a person waiting would say it. */
export function shortDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}
