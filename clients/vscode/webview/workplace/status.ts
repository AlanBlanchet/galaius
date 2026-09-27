/** ONE STATUS VOCABULARY, for the building and the desk.
 *
 *  Both panels shared sprites, palette, pod colours — and still said the same fact two ways.
 *  Sidebar's loudest device was a stamped word (ERROR, DONE, HELD, NOT OURS) on a work order; the
 *  building said it with a 6px nameplate mark and a blinking wedge, words only in a bottom
 *  legend, no text-badge vocabulary at all.
 *
 *  Two things drifted, not one: SHAPES agreed, WORDS didn't — where the building had any ("stuck"
 *  for ERROR, "finished" for DONE, "idle 2m+" for HELD), they were different words. Same defect
 *  as two colour allocators for one set of colours.
 *
 *  So the vocabulary lives here once: which states are worth saying, what each is CALLED, what
 *  SHAPE marks it, what the stamp is made of. Both surfaces import it; neither gets a second
 *  opinion.
 *
 *  NOT shared: where the stamp lands. Desk: ink pressed into paper, outline, no shadow. Building:
 *  a placard over someone's head, opaque, the same hard shadow every sign there throws. Same
 *  word, box, tilt, colours — mounted the way each place mounts things.
 */
import { draw } from "./pixels";
import { MARKS } from "./art";
import { STATUS, type Attention } from "../../src/statusLanguage";

/** Past two minutes of nothing, a worker isn't working. Both surfaces used to declare this
 *  privately — precisely how two views end up disagreeing who's stalled. */
export const STALL_SECONDS = 120;

/** States worth saying out loud. WORKING isn't one: an open job with nothing stamped is the
 *  oldest "in progress" signal there is, and leaving it unstamped keeps the loud marks meaning
 *  something.
 *
 *  These are Attention values, not a private enum — the building used to name its own four
 *  states (done/error/held/foreign) beside a rail naming six: same taxonomy, two spellings, one
 *  product looking like two.
 */
export type StampKind = Exclude<Attention, "working" | "ready">;

export interface Stamp {
  kind: StampKind;
  /** The word, as printed. Comes from statusLanguage, never from here. */
  word: string;
  /** Which mark in MARKS carries this state as a SHAPE. Rail's mark is a TEXT glyph, but webview
   *  fonts don't carry half of them (six identical tofu boxes, last attempt) — so the world draws
   *  the same MEANING in its own pixels. Word and accent are shared; only the shape's rendering
   *  is each surface's own. */
  mark: string;
  /** The theme variable this state is tinted with, on BOTH surfaces. */
  accent: string;
}

/** Which pixel drawing carries each state. art.ts owns the shapes; this is the only place that
 *  says which shape means which state. */
const SHAPES: Record<StampKind, string> = {
  error: "error",
  asked: "asked",
  held: "held",
  finished: "done",
  "not-ours": "foreign",
  /* A filled block: the tape-deck stop, the one mark that means "a person ended this". */
  stopped: "stopped",
};

function stampOf(kind: StampKind): Stamp {
  const voice = STATUS[kind];
  return { kind, word: voice.word, mark: SHAPES[kind], accent: voice.accent };
}

export const STAMPS: Record<StampKind, Stamp> = {
  error: stampOf("error"),
  asked: stampOf("asked"),
  held: stampOf("held"),
  finished: stampOf("finished"),
  "not-ours": stampOf("not-ours"),
  stopped: stampOf("stopped"),
};

/** What a counter calls each state. Lowercase — a tally is prose, a stamp is a stamp — but the
 *  SAME word underneath, the whole point of this table. */
export const WORDS = {
  running: "on",
  done: STATUS.finished.word.toLowerCase(),
  error: STATUS.error.word.toLowerCase(),
  held: STATUS.held.word.toLowerCase(),
  foreign: STATUS["not-ours"].word.toLowerCase(),
} as const;

/** Anything either surface can hand this module. Neither view's row type is imported, so this
 *  file cannot drag a view model across the seam. */
export interface Standing {
  status: string;
  idle_seconds: number;
}

/** THE rule for what a run shows, in one place — produces the RAIL's vocabulary, not a second
 *  one.
 *
 *  Order matters: a dead run is ERROR even after an hour, only a RUNNING one can be HELD.
 *  Sleeping a finished or foreign run would say "stuck" when it's simply over — the building
 *  used to snooze a DONE worker like that; the desk never did.
 *
 *  ASKED is deliberately unreachable here — a DATA gap, not an omission: rail gets
 *  awaitingReply from the run record, workplace's view model never carried it. State stays
 *  declared so the world can draw it once the field arrives.
 */
export function attentionOf(w: Standing): Attention {
  if (w.status === "error") return "error";
  if (w.status === "foreign") return "not-ours";
  /* Declared in the company, never asked. Reached only once floor status carries it — floorStatus
     used to collapse declared into done, conflating "waiting for first job" with "did its job";
     posture system tells them apart. */
  if (w.status === "ready") return "ready";
  if (w.status === "done") return "finished";
  return w.idle_seconds >= STALL_SECONDS ? "held" : "working";
}

export function stampFor(w: Standing): Stamp | null {
  const a = attentionOf(w);
  // Working and ready carry no placard: one is busy, the other has simply not been asked yet.
  return a === "working" || a === "ready" ? null : STAMPS[a];
}

/* ── the state a BODY carries, not a placard ─────────────────────────────────
 *
 *  "All the agents say finished whereas we don't care... they could have a seat or rest in
 *  their room."
 *
 *  Stamp was a good device, spent on everything: four of five states got the same treatment as
 *  the one that wants somebody NOW, so finished work shouted as loud as a crash.
 *
 *  So ordinary states move into the BODY; placard stays only for the two that want a human:
 *
 *      ERROR     stand, out of the chair, room lit red.       STAMPED.
 *      ASKED     stand at the desk, turned to the door.       STAMPED.
 *      WORKING   sit at the desk, typing.                     no stamp.
 *      HELD      same seat, head down, eyes shut.              no stamp.
 *      FINISHED  walked to the rest end, sat down.             no stamp.
 *      NOT OURS  visitor, sitting at the rest end.             no stamp.
 *
 *  Nothing deleted from the vocabulary: rail still prints all six words/marks/accents, stampFor
 *  above still answers for all six (a list has no posture to spend). This is the WORLD's reading
 *  of the same table.
 */
export type Posture = "sit" | "slump" | "lounge" | "stand";

export interface Behaviour {
  /** Which sprite pair the body rests in. */
  posture: Posture;
  /** Which end of its room it occupies. */
  post: "desk" | "rest";
  /** Whether the world hangs a placard over it. */
  stamp: boolean;
}

const BEHAVIOUR: Record<Attention, Behaviour> = {
  working: { posture: "sit", post: "desk", stamp: false },
  held: { posture: "slump", post: "desk", stamp: false },
  error: { posture: "stand", post: "desk", stamp: true },
  asked: { posture: "stand", post: "desk", stamp: true },
  finished: { posture: "sit", post: "rest", stamp: false },
  "not-ours": { posture: "sit", post: "rest", stamp: false },
  /* Declared, never asked: STANDING AT ITS DESK, no placard/caption/fade. Posture alone measured
     imperceptible beside loungers, so the distinction is position AND posture together: done work
     sits at the rest end, still-waiting stands at its station. Thirty idle-declared agents at
     their desks reads as a staffed floor waiting for work — the honest picture of a big roster. */
  ready: { posture: "stand", post: "desk", stamp: false },
  /* Killed by a person: stands at its desk wearing the word — not success, not an alarm. */
  stopped: { posture: "stand", post: "desk", stamp: true },
};

export function behaviourOf(a: Attention): Behaviour {
  return BEHAVIOUR[a] ?? BEHAVIOUR.working;
}

/** How tall a body in each posture is ON SCREEN, device pixels: grid rows plus derived rim,
 *  times sprite scale.
 *
 *  Stated ONCE because four things must agree with it to the pixel — nameplate and placard
 *  (hang off the head), speech layout's reserved rectangles (promise no two labels overlap), and
 *  the stylesheet. Were four hand-tuned constants for one sprite height; a posture changing
 *  height by 12px silently invalidated all four — a name floating above the head, a reserve box
 *  refusing bubbles over empty air while allowing one over a word. */
export const HEAD: Record<Posture, number> = {
  /* A Kenney doll is a 16px frame drawn at 2x: 32 on screen, boots on the anchor. */
  stand: 32,
  /* Seated tucks the figure four pixels toward its desk. */
  sit: 28,
  slump: 28,
  /* Rotated onto the couch — the figure lies, so its top is the doll's WIDTH. */
  lounge: 22,
};

/** The stamp the WORLD hangs, not the rail's stamp. Two states earn a placard over somebody's
 *  head; the rest are said by what the body is doing. */
export function worldStampFor(w: Standing): Stamp | null {
  const a = attentionOf(w);
  return behaviourOf(a).stamp ? STAMPS[a as StampKind] : null;
}

/** Only work can stall. Used by both surfaces for the sleep mark and the stopped ambient. */
export function isHeld(w: Standing): boolean {
  return w.status === "running" && w.idle_seconds >= STALL_SECONDS;
}

/** The mark for a state, drawn through the workplace's own pixel engine — the same rectangles in
 *  both views, not a lookalike. */
export function markOf(status: string, scale = 2): string {
  const piece = MARKS[status] ?? MARKS.foreign;
  return draw(piece.grid, piece.pal, { scale, outline: false });
}

/** The stamp itself. One element, one class, both panels. */
export function stampHtml(s: Stamp, scale = 2): string {
  return (
    `<span class="wp-stamp" data-kind="${s.kind}" style="--voice:var(${s.accent})">` +
    `${markOf(s.mark, scale)}<b>${s.word}</b></span>`
  );
}

/** The stamp, as CSS, embedded verbatim by both stylesheets.
 *
 *  Everything that makes the device recognisable is fixed here — 2px rule, tilt, uppercase
 *  tracking, weight, four colour treatments. Each panel supplies only how it's MOUNTED on its
 *  own substrate, through five custom properties:
 *
 *    --stamp-ink     substrate's own near-black, mixed into every outline colour
 *    --stamp-quiet   substrate's dim text colour, default when a kind sets nothing
 *    --stamp-mix     how much status hue survives that mix, per theme
 *    --stamp-bg      what's behind the letters (paper: nothing; a room: the panel colour)
 *    --stamp-shadow  a placard casts one; ink pressed into paper doesn't
 *
 *  ERROR is fully specified here, no substrate knob: red on either stock can't reach the text
 *  floor while still reading as red — measured 3.3-3.6:1 in dark by two independent methods — so
 *  it becomes a filled plate, mixed DOWN toward near-black red so white-on-it holds in both
 *  themes.
 */
export const STAMP_CSS = String.raw`
.wp-stamp {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 4px 2px;
  border: 2px solid currentColor;
  color: var(--stamp, var(--stamp-quiet, currentColor));
  background: var(--stamp-bg, transparent);
  box-shadow: var(--stamp-shadow, 0 0 0 0 transparent);
  --mark: currentColor;
  /* 10px, not 9 — a rotated 2px outline is mostly anti-aliased edge, so it measured weaker than
     text beside it at the same colour. */
  font-size: var(--stamp-size, 10px);
  font-weight: 700;
  line-height: 1;
  letter-spacing: .11em;
  white-space: nowrap;
  text-transform: uppercase;
  transform: rotate(-7deg);
}
.wp-stamp svg { display: block; }
.wp-stamp[data-kind="finished"], .wp-stamp[data-kind="asked"] {
  --stamp: color-mix(in srgb, var(--voice) var(--stamp-mix, 52%), var(--stamp-ink));
}
.wp-stamp[data-kind="not-ours"], .wp-stamp[data-kind="held"] { --stamp: var(--stamp-ink); }
.wp-stamp[data-kind="error"] {
  --stamp: #fff;
  background: color-mix(in srgb, var(--voice) 58%, #1a0508);
  border-color: color-mix(in srgb, var(--voice) 58%, #1a0508);
  /* The one state that wants a person NOW keeps motion — a halo, not a blink: blinking to 15%
     opacity makes the one word that matters unreadable half the time. Plate holds full
     strength, only the alarm ring moves — on the beat, like everything else here. */
  animation: wp-stamp-alarm calc(var(--beat) * 2 / 3) ease-in-out infinite;
}
@keyframes wp-stamp-alarm {
  0%, 100% { box-shadow: var(--stamp-shadow, 0 0 0 0 transparent), 0 0 0 0 color-mix(in srgb, var(--voice) 65%, transparent); }
  50% { box-shadow: var(--stamp-shadow, 0 0 0 0 transparent), 0 0 0 5px color-mix(in srgb, var(--voice) 0%, transparent); }
}
@media (prefers-reduced-motion: reduce) {
  .wp-stamp[data-kind="error"] { animation: none; }
}
`;
