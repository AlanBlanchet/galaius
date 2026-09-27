/** The rail, rendered.
 *
 *  Everything here exists at REST — no hover, no focus, no agent selected. That is the entire
 *  reason this surface is a webview rather than a `TreeView`: VS Code shows a view's title actions
 *  only while the pointer is inside the header and clips the overflow with no "…" menu, so the
 *  panel it replaces rendered zero buttons until you happened to move the mouse over it.
 *
 *  Two host facts this file is shaped by, both learned the hard way:
 *   - the theme variables are injected onto `<body>`, never `:root`, so a `:root` token block
 *     silently never resolves;
 *   - a `<script>` without the CSP nonce is dropped without a word, taking every behaviour with it.
 */

import type { Rail, RailHeader } from "./rail";
import type { AgentAction } from "./agentActions";

/** Kept in step with `themeTokens.ts` DIM_FOREGROUND, and inlined rather than imported so this
 *  module stays runtime-import-free for the test loader. `--vscode-descriptionForeground` is
 *  #767676 on Light+'s #f8f8f8 — 4.28:1, under AA before a panel touches it — so the raw token is
 *  never used for text. Measured here: 4.3:1 raw, 5.4:1 pulled toward the foreground. */
const DIM = "color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 70%, " +
  "var(--vscode-editor-foreground, #d4d4d4))";

/** The header as one line of prose — presentation, so it lives here rather than in `rail.ts`.
 *  That also keeps this module free of any RUNTIME import: the type above is erased, which is what
 *  lets the test loader resolve it (it demands ".ts" specifiers that tsc refuses to emit).
 *
 *  An empty team reads as a sentence rather than "0 · 0 · 0", which looks like a fault instead of
 *  a quiet morning.
 */
export function headerLine(header: RailHeader): string {
  const bits: string[] = [];
  if (header.needsYou) bits.push(`${header.needsYou} need${header.needsYou === 1 ? "s" : ""} you`);
  if (header.working) bits.push(`${header.working} working`);
  if (header.finished) bits.push(`${header.finished} done`);
  return bits.length ? bits.join(" · ") : "nobody working yet";
}

function esc(text: string): string {
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** How a status is spoken. Shape first, colour second — colour alone is unreadable to a good share
 *  of people and invisible in a screenshot taken at a glance.
 *
 *  This used to be a local table, and that was the defect: the world stamped "DONE" on tilted paper
 *  while this row said a green tick and lowercase "finished". Same five states, two products —
 *  which visual-critic called out twice and tied to "the environment is weird". The vocabulary now
 *  lives in `statusLanguage.ts` and is PASSED IN rather than imported, because this module must stay
 *  free of runtime imports for the test loader. Required, not optional: a default here would let a
 *  caller quietly reintroduce a second source of truth, and the compiler is a better guard than a
 *  comment asking nicely.
 */
export type Voice = { mark: string; word: string; phrase: string; accent: string; tinted: boolean; quiet?: boolean };

export function railHtml(
  rail: Rail,
  nonce: string,
  /** How each status is spoken — the SAME vocabulary the workplace stamps. See `statusLanguage.ts`. */
  voiceOf: (attention: string) => Voice,
  /** What to CALL a conversation. Passed in (this module stays runtime-import-free) from
   *  `conversation.ts`, which names a conversation by what it is DOING — the old rule fell back to
   *  the provider, so twenty unrelated engagements all rendered as "claude". */
  titleOf: (run: Rail["runs"][number]["run"]) => string,
  /** Which AGENT held a conversation — its stable `id` (what the filter matches on) and its
   *  `label` (what a person reads). Two fields, because the coordinator's id is "main" while its
   *  label is its title: posting the label as the filter key would match nothing. From
   *  `roster.ts`; passed in, never imported. */
  roleOf: (run: Rail["runs"][number]["run"]) => { id: string; label: string },
  /** What each row can be asked to do. Passed in rather than imported so this module keeps no
   *  runtime import — the test loader demands ".ts" specifiers that tsc refuses to emit. */
  actionsFor: (run: Rail["runs"][number]["run"]) => AgentAction[] = () => [],
  /** How competent that colleague's model is measured — see railBody's scoreOf param for why. */
  scoreOf: (run: Rail["runs"][number]["run"]) => { text: string; title: string } | undefined =
    () => undefined,
  /** What that colleague last ran on — see railBody's modelOf param. */
  modelOf: (run: Rail["runs"][number]["run"]) => string | undefined = () => undefined,
): string {
  return railDocument(nonce, railStyle(),
    railBody(rail, voiceOf, titleOf, roleOf, actionsFor, scoreOf, modelOf));
}

/** The rail's stylesheet, on its own so a HOST document can embed the roster.
 *
 *  The roster's home is the big editor panel, beside the world — "they should be in the big main
 *  panel somewhere". A side-bar view renders a whole document; an editor panel needs a FRAGMENT it
 *  can place next to the room. Splitting document from fragment is what lets one roster serve both
 *  without a second implementation drifting away from this one.
 */
export function railStyle(): string {
  return `/* THE STAFF BOARD ON THE OFFICE WALL.
   The world beside this column is vivid Kenney pixel art; a native VS Code list bolted to it read
   as two products. So the roster is built from the world's own kit: its ink (#14101c), its hard
   2px-offset shadows, its plate-and-plaque signage, its press-into-the-shadow buttons, its stamp
   (2px rule, tracked uppercase, the error plate with the alarm halo, on the same 2.4s beat), and
   the atlas's own measured hues — lawn teal #38cbab / #21837c, terracotta #c66527, furniture wood
   #8f673f. Literal tones are mixed toward theme ink/paper (never toward the page), so the same
   plate holds AA in Dark+ and Light+ — every pairing below was measured, not eyeballed.
   Tokens hang off body: VS Code injects --vscode-* there, and a :root block never resolves. */
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0;
  font-family: var(--vscode-font-family);
  --bd-text-body: max(15px, var(--vscode-font-size, 13px));
  --bd-text-small: max(15px, var(--vscode-font-size, 13px));
  font-size: var(--bd-text-body);
  line-height: 1.5;
  color: var(--vscode-foreground);
  background: var(--vscode-sideBar-background, var(--vscode-editor-background));

  /* The board's palette, sampled from the world's own atlas + style. Namespaced --bd-* so the
     embedded fragment never collides with the workplace's --wp-* set on the same page. */
  --bd-beat: 2.4s;                                  /* the ONE beat everything ambient shares */
  --bd-ink: #14101c;                                /* the world's ink — every hard shadow */
  --bd-teal: #38cbab;                               /* the lawn, bright */
  --bd-deep: #21837c;                               /* the lawn, in shade */
  --bd-terra: #c66527;                              /* roofs and furniture */
  --bd-wood: #8f673f; --bd-wood-hi: #b98b5e;        /* the desks */
  --bd-paper: #f3efe4;                              /* the world's paper stock */
  --bd-bg: var(--vscode-editor-background, #1f1f1f);
  --bd-fg: var(--vscode-foreground, #cccccc);
  --bd-line: color-mix(in srgb, var(--bd-fg) 26%, var(--bd-bg));      /* --wp-line's recipe */
  --bd-chrome: color-mix(in srgb, var(--bd-bg) 92%, var(--bd-fg));    /* --wp-chrome's recipe */
  --bd-shadow: color-mix(in srgb, var(--bd-ink) 78%, transparent);
  /* Plates mix the hue toward INK, never toward the page: a teal plate mixed toward white died at
     1.9-3.1:1 in Light+, while these hold in both themes because they barely move. Measured:
     paper-on-teal-plate 6.8:1, paper-on-wood-plate 6.2:1, both themes. */
  --bd-plate: color-mix(in srgb, var(--bd-deep) 65%, var(--bd-ink));
  --bd-woodplate: color-mix(in srgb, var(--bd-wood) 75%, var(--bd-ink));
  --bd-papertext: color-mix(in srgb, var(--bd-paper) 92%, var(--bd-bg));
  /* The header's teal wash and the cap-light on it. 26% keeps DIM at 5.4:1 in dark; Light+ drops
     to 18% below because 26% took DIM to 4.2 — under the floor. */
  --bd-wash: color-mix(in srgb, var(--bd-deep) 26%, var(--bd-bg));
  --bd-cap: color-mix(in srgb, var(--bd-teal) 40%, transparent);
  --bd-grid: color-mix(in srgb, var(--bd-fg) 5%, transparent);
  --bd-hover: color-mix(in srgb, var(--bd-deep) 15%, var(--bd-bg));

  /* The floor's own tile grid, as a whisper — texture, never noise behind body text. */
  background-image:
    linear-gradient(var(--bd-grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--bd-grid) 1px, transparent 1px);
  background-size: 16px 16px;
}
body.vscode-light, body.vscode-high-contrast-light {
  --bd-wash: color-mix(in srgb, var(--bd-deep) 18%, var(--bd-bg));    /* DIM 4.68:1, measured */
  --bd-cap: color-mix(in srgb, var(--bd-deep) 35%, transparent);      /* bright teal is 2:1 on white */
  --bd-grid: color-mix(in srgb, var(--bd-fg) 6%, transparent);
}
/* Inside the split document the host paints .wp-list with a bare background shorthand; the tag
   qualifiers out-rank it so the board surface and its 2px frame seam survive there too. */
div.wp-split > div.wp-list {
  background-color: var(--bd-bg);
  background-image:
    linear-gradient(var(--bd-grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--bd-grid) 1px, transparent 1px);
  background-size: 16px 16px;
  border-left: 2px solid var(--bd-plate);
}
/* The board scrolls behind a square rail, not a rounded native thumb. The ROOT scroller's
   pseudo-elements resolve variables against <html>, which the body-scoped tokens never reach —
   so the root gets var-free neutrals and any scroller INSIDE body gets the board's own inks. */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-corner { background: transparent; }
::-webkit-scrollbar-thumb {
  background: rgba(125, 125, 125, .4);
  border: 2px solid transparent; background-clip: padding-box;
}
body ::-webkit-scrollbar-thumb {
  background: color-mix(in srgb, var(--bd-fg) 24%, var(--bd-bg));
  border: 2px solid var(--bd-bg); background-clip: border-box;
}
body ::-webkit-scrollbar-thumb:hover {
  background: color-mix(in srgb, var(--bd-deep) 55%, var(--bd-bg));
}
::selection { background: color-mix(in srgb, var(--bd-teal) 35%, var(--bd-bg)); }
/* The board's painted top rail: teal wash, a cap-light along its top edge (the world lights every
   wall cap), and the deep-teal foot it stands on. Opaque, because the list scrolls under it. */
.rail {
  position: sticky; top: 0; z-index: 2;
  padding: 9px 10px 8px;
  background-color: var(--bd-wash);
  border-bottom: 2px solid var(--bd-plate);
  box-shadow: inset 0 2px 0 var(--bd-cap);
}
/* The scope is the answer to "whose agents are these?" — so it is also the way to change it.
   It is the board's routed WOOD nameplate: the one wooden thing here, like the desks are there.
   Paper text on the wood plate measures 6.2:1 in both themes. */
.scope {
  font: inherit; font-weight: 700; cursor: pointer;
  padding: 2px 8px; border-radius: 0;
  color: var(--bd-papertext);
  background: var(--bd-woodplate);
  /* A routed edge: lit on top/left, in shade below — the pixel bevel every Kenney prop carries. */
  border: 1px solid;
  border-color: color-mix(in srgb, var(--bd-wood-hi) 55%, var(--bd-woodplate))
    color-mix(in srgb, var(--bd-ink) 45%, var(--bd-woodplate))
    color-mix(in srgb, var(--bd-ink) 45%, var(--bd-woodplate))
    color-mix(in srgb, var(--bd-wood-hi) 55%, var(--bd-woodplate));
  box-shadow: 2px 2px 0 var(--bd-shadow);
}
.scope::after { content: " ⌄"; opacity: .8; font-size: var(--bd-text-small); }
.scope:hover { background: color-mix(in srgb, var(--bd-wood) 88%, var(--bd-ink)); }
.scope:active { transform: translate(1px, 1px); box-shadow: 1px 1px 0 var(--bd-shadow); }
.scope:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 1px; }
/* Stronger than DIM: this line sits ON the header's teal wash, which eats ~0.3 of ratio —
   measured 4.2:1 in Light+ with the plain DIM mix, under the 4.5 floor. 85% toward the
   foreground clears it in both themes while still reading quieter than the scope. */
.counts { color: color-mix(in srgb, var(--vscode-foreground, #cccccc) 85%, ` +
  `var(--vscode-descriptionForeground, #9a9a9a)); margin-left: 7px; }
/* THE TWO VIEWS. One row model, two layouts — the view is a property of the CONTAINER, so
   nothing about a row has to know which shape it is wearing today.

   GROUPED is the roster as it always was: live work, history and staff in their own sections.
   TABLE puts every row on ONE grid template, so columns line up down the whole list and a long
   roster reads as a comparison instead of a stack. */
/* Its own query container, so the table's stand-down works wherever the roster is embedded —
   the workplace panel and the standalone rail document both. */
.roster {
  container-type: inline-size;
  --bd-row-columns:
    18px minmax(9rem, 2fr) minmax(6.5rem, .9fr) minmax(9rem, 2fr)
    minmax(5rem, .8fr) minmax(7rem, 1.2fr) minmax(4.5rem, .8fr)
    minmax(5rem, .8fr) minmax(5rem, .8fr) minmax(4rem, .6fr) minmax(15rem, 1.4fr);
}
.views { display: flex; gap: 2px; margin: 4px 0 2px; }
.views .view {
  font: inherit; font-size: var(--bd-text-small); cursor: pointer; padding: 1px 8px; min-height: 32px;
  color: ${DIM}; background: transparent;
  border: 1px solid var(--vscode-panel-border, transparent); border-radius: 5px;
}
.views .view:hover { background: var(--vscode-list-hoverBackground); }
.views .view[aria-pressed="true"] {
  color: var(--vscode-foreground); border-color: var(--vscode-focusBorder);
}
.views .view:focus-visible { outline: 1px solid var(--vscode-focusBorder); }

/* The name column takes the slack and may shrink to nothing, so a long agent id ellipsises
   rather than pushing the measure off the end — a column that only lines up for short names
   is not a column. */
.runs .row, .columnHead {
  display: grid; align-items: center; gap: 6px 10px;
  /* The rail is usually a wide editor tab, not a 300px side bar. Give every fact its own track so
     provider, model, effort, score, time, cost and actions stay visible and line up down the list.
     The name track takes the remaining width; fixed minimums stop optional fields disappearing
     when one task title is long. */
  grid-template-columns: var(--bd-row-columns);
  min-width: 0;
}
.view-table .runs .row {
  /* TABLE keeps the same wide tracks explicitly; GROUPED uses them too so switching views does
     not make status and actions appear or vanish. */
  display: grid;
  /* FIXED trailing tracks. An auto track sizes per ROW, so each row picked its own widths and
     the score cell landed at 8 different x positions across 267px — a column that does not line
     up is not a column. These widths are shared by every row, so the list reads down. */
  grid-template-columns: var(--bd-row-columns);
}
.columnHead {
  padding: 4px 10px 2px;
  color: ${DIM}; font-size: var(--bd-text-small); letter-spacing: .06em; text-transform: uppercase;
  border-bottom: 1px solid var(--bd-line);
}
.columnHead span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-identity, .run-facts { display: contents; }
.runs .who {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 4.5em;
}
.runs .provider, .runs .model, .runs .effort, .runs .score, .runs .when, .runs .cost {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* The task sentence owns its column, rather than stealing the next row's space. */
.runs .oneliner {
  grid-column: 4;
  grid-row: 1;
  padding-left: 0;
}

/* CARDS REMOVED. Measured against the same 75 records it was 1.735x the height of grouped
   (8789px vs 5065px) for identical fields, with the dismiss control alone on its own line. A view
   that shows the same things in nearly twice the scrolling is not a third option, it is a worse
   one — so it is gone rather than kept for symmetry. Empty desktop cells stay in the grid: hiding
   one would shift every later fact left in that row and break column alignment. */
/* A table needs width. Below this it stops being a comparison — 112px of horizontal overflow and
   seven-character names — so it stands down to the stacked reading rather than overflowing. */
@container (max-width: 1380px) {
  .runs .row, .view-table .runs .row {
    display: grid; grid-template-columns: minmax(13rem, .7fr) minmax(0, 1.5fr);
    align-items: start; gap: 8px 18px; padding-block: 10px;
  }
  .run-identity { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; grid-column: 1; grid-row: 1; min-width: 0; }
  .runs .who { flex: 0 1 auto; font-weight: 650; }
  .runs .oneliner { grid-column: 2; grid-row: 1; color: var(--bd-fg); }
  .run-facts { display: flex; flex-wrap: wrap; gap: 4px 12px; grid-column: 1; grid-row: 2; min-width: 0; }
  .runs .provider, .runs .model, .runs .effort, .runs .score, .runs .when, .runs .cost {
    flex: 0 1 auto; max-width: 100%;
  }
  .runs .acts { grid-column: 2; grid-row: 2; display: flex; flex-wrap: wrap; justify-self: start; }
  .runs .row > :empty, .run-facts > :empty { display: none; }
  .columnHead { display: none; }
}
@container (max-width: 800px) {
  .runs .row, .view-table .runs .row { grid-template-columns: minmax(0, 1fr); }
  .runs .oneliner { grid-column: 1; grid-row: 2; }
  .run-facts { grid-column: 1; grid-row: 3; }
  .runs .acts { grid-column: 1; grid-row: 4; justify-self: start; }
}

.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
/* The world's own latch button (.wp-cam-wide): square, hard ink shadow, and a press that moves
   the button INTO its shadow — the one depth cue pixel art allows itself. */
.chip {
  font: inherit;
  padding: 3px 10px;
  border-radius: 0;
  cursor: pointer;
  color: var(--bd-fg);
  background: var(--bd-chrome);
  border: 1px solid var(--bd-line);
  box-shadow: 2px 2px 0 var(--bd-shadow);
}
.chip:hover {
  background: color-mix(in srgb, var(--bd-deep) 22%, var(--bd-bg));
  border-color: color-mix(in srgb, var(--bd-deep) 55%, var(--bd-line));
}
.chip:active { transform: translate(1px, 1px); box-shadow: 1px 1px 0 var(--bd-shadow); }
.chip:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 1px; }
/* A slot cut into the board: square, grooved by an inset ink shadow rather than a native well. */
.seek {
  font: inherit; width: 100%; margin-top: 9px; padding: 4px 9px; border-radius: 0;
  color: var(--vscode-input-foreground, var(--bd-fg));
  background: color-mix(in srgb, var(--bd-fg) 5%, var(--bd-bg));
  border: 1px solid var(--bd-line);
  box-shadow: inset 2px 2px 0 color-mix(in srgb, var(--bd-ink) 20%, transparent);
}
.seek::placeholder { color: ${DIM}; opacity: 1; }
.seek::-webkit-search-cancel-button { -webkit-appearance: none; appearance: none; }
.seek:focus { border-color: color-mix(in srgb, var(--bd-deep) 70%, var(--bd-fg)); }
.seek:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
.crumbs { display: flex; align-items: baseline; gap: 5px; margin-top: 7px; font-size: var(--bd-text-small); }
.crumb {
  font: inherit; cursor: pointer; padding: 0 5px; border-radius: 0;
  color: var(--bd-teal); background: transparent; border: 1px solid transparent;
}
.crumb:hover { background: var(--bd-hover); border-color: var(--bd-line); }
/* Bright teal is a dark-theme colour; on paper it drops to the lawn's shade tone (4.6:1). */
body.vscode-light .crumb, body.vscode-high-contrast-light .crumb { color: var(--bd-deep); }
.crumbSep { color: ${DIM}; }
.crumbNow { font-weight: 700; }
/* The role a conversation was held with. Quiet, but a real control: it is how you get from one
   engagement to every other engagement with the same colleague. */
.role {
  font: inherit; font-size: var(--bd-text-small); cursor: pointer;
  padding: 0 5px; border-radius: 0; white-space: nowrap;
  color: ${DIM}; background: transparent;
  border: 1px solid var(--bd-line);
}
.role:hover { color: var(--bd-fg); background: var(--bd-hover); }
.role:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
/* The ×N task counter used to render as a BARE NATIVE BUTTON — the loudest "IDE widget" on the
   whole board, and no rule here ever addressed it. A small pixel counter chip now. */
.tasks {
  font: inherit; font-size: var(--bd-text-small); line-height: 1.2; cursor: pointer;
  margin-left: 5px; padding: 0 4px; border-radius: 0;
  color: var(--bd-fg); background: var(--bd-chrome);
  border: 1px solid var(--bd-line);
  box-shadow: 1px 1px 0 var(--bd-shadow);
}
.tasks:hover { background: color-mix(in srgb, var(--bd-deep) 22%, var(--bd-bg)); }
.tasks:active { transform: translate(1px, 1px); box-shadow: none; }
ul.runs { list-style: none; margin: 0; padding: 4px 0; }
/* FLEX, not grid, and deliberately so. This row was a 4-track grid; the round that added a stamp
   and a role chip took it to six children, so .note landed in the 16px mark column and broke one
   letter-pair per line ("st/op/pe/d/wi/th"). A grid whose track count must be kept in sync by hand
   with a template's child count is a trap that renders perfectly in every unit test and is
   unreadable on screen. Flex wrapping cannot fall out of step: children take the room they need,
   and the two full-width rows say so themselves. */
.score {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  opacity: .8;
  white-space: nowrap;
}

.legend {
  font-size: var(--bd-text-small);
  letter-spacing: .04em;
  /* Legible: at .6 it measured 3.73:1, under the 4.5:1 floor, on the one line that says what the
     numbers beneath it are. */
  opacity: .85;
  margin: 2px 0 4px;
}

.ranon {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: var(--bd-text-small);
  opacity: .7;
}

.row {
  /* The grid above owns the desktop shape. Keep this declaration as the narrow fallback's base
     contract and as a safe default if the embedded stylesheet is loaded without container queries. */
  display: grid; align-items: center;
  gap: 6px 10px;
  padding: 3px 10px; cursor: pointer;
  /* The anchor for row-level controls and keyboard focus. */
  position: relative;
}
.mark { width: 18px; text-align: center; }
.stamp, .role { flex: none; }
/* The same STAMP the world presses: the fixed 2px rule, the tracked uppercase, a slight tilt —
   ink pressed into the board, so no shadow and nothing behind it (that is how a desk mounts it;
   only the placard in the building throws a shadow). -3deg, not the placard's -7: a dense row
   grants about a pixel of overhang, and the tilt only has to say "pressed by hand". */
.stamp {
  display: inline-flex; align-items: center;
  font-size: var(--bd-text-small); font-weight: 700; letter-spacing: .11em; line-height: 1.1;
  padding: 1px 4px 2px; border-radius: 0; white-space: nowrap;
  color: var(--accent, var(--bd-fg));
  border: 2px solid currentColor;
  background: transparent;
  transform: rotate(-3deg);
}
/* Chart accents are dark-theme colours; on paper they thin out (yellow measured 2.9:1), so the
   light themes pull every accent toward the foreground — the DIM discipline, applied to hue. */
body.vscode-light .stamp, body.vscode-high-contrast-light .stamp {
  color: color-mix(in srgb, var(--accent, var(--bd-fg)) 58%, var(--bd-fg));
}
body.vscode-light .mark, body.vscode-high-contrast-light .mark {
  color: color-mix(in srgb, var(--accent, var(--bd-fg)) 65%, var(--bd-fg));
}
.row:hover { background: var(--bd-hover); }
/* The two states that want a person carry the room-rim's device: an accent edge on the row. */
.runs .row[data-attention="error"], .runs .row[data-attention="asked"] {
  box-shadow: inset 3px 0 0 var(--accent);
}
/* ERROR is the world's filled plate — white on near-black red, the one pairing that holds in both
   themes — with the same alarm halo, on the same beat, that pulses over a crashed head next door. */
.runs .row[data-attention="error"] .stamp {
  color: #fff;
  background: color-mix(in srgb, var(--accent) 58%, #1a0508);
  border-color: color-mix(in srgb, var(--accent) 58%, #1a0508);
  animation: bd-alarm calc(var(--bd-beat) * 2 / 3) ease-in-out infinite;
}
@keyframes bd-alarm {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 65%, transparent); }
  50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--accent) 0%, transparent); }
}
@media (prefers-reduced-motion: reduce) {
  .runs .row[data-attention="error"] .stamp { animation: none; }
}
/* Actions are part of each wide row's information, not hover-only chrome. They remain compact and
   use the same label as their title, so a person can understand the control without guessing at a
   glyph or moving the pointer. Filtered to the actions that would WORK, so nothing here is dead. */
.acts {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px;
  opacity: 1; pointer-events: auto;
  position: static; transform: none;
  min-width: 0; overflow: hidden; justify-content: flex-end;
  padding: 1px 3px; border-radius: 0;
  /* A little tool tray that arrives with the hand: chrome stock, hard ink shadow, like the
     survey instruments in the corner of the room next door. */
  background: var(--bd-chrome);
  border: 1px solid var(--bd-line);
  box-shadow: 2px 2px 0 var(--bd-shadow);
}
.act {
  font: inherit; line-height: 1.35; padding: 5px 7px; min-width: 0; min-height: 32px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  border: 1px solid transparent; border-radius: 0;
  background: transparent; color: ${DIM}; cursor: pointer;
}
.actText { margin-left: 3px; }
.act:hover { background: color-mix(in srgb, var(--bd-deep) 26%, var(--bd-bg));
  color: var(--bd-fg); border-color: color-mix(in srgb, var(--bd-deep) 45%, var(--bd-line)); }
.act:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
/* ONE line. A task is a sentence and a sentence is a paragraph in a 300px column — the owner's
   panel read as walls of "Reply with exactly: ..." because titles were allowed to wrap. Clipped
   with an ellipsis; the row opens for the rest. */
.who {
  font-weight: 500; min-width: 0; flex: 1 1 auto;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.when, .cost { flex: none; color: ${DIM}; font-size: var(--bd-text-small); }
.provider, .model, .effort { color: ${DIM}; font-size: var(--bd-text-small); }
/* The grouped row's second line: what the speaking errand is about, clipped the same way. */
.oneliner {
  min-width: 0; color: ${DIM}; font-size: var(--bd-text-small);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.task-detail > summary { cursor: pointer; display: flex; align-items: baseline; gap: 6px; min-height: 24px; }
.task-detail > summary::before { content: "▸"; flex: none; }
.task-detail[open] > summary::before { content: "▾"; }
.task-detail > summary > span { flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; }
.task-disclosure-label { flex: none; font-weight: 500; color: var(--bd-teal); text-decoration: underline; text-underline-offset: 3px; }
.task-detail > summary:focus-visible { outline: 2px solid var(--vscode-focusBorder); outline-offset: -2px; }
.task-detail > p { white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 0; line-height: 1.5; color: var(--bd-fg); }
/* Band headings are PLAQUES — the signs the building hangs on every department, pressed onto the
   board with a terracotta pushpin. Deep-teal plate mixed toward ink so paper text holds 6.8:1 in
   both themes; the same hard shadow every sign in the building throws. */
.staffHead, details.staffBox > summary.staffHead {
  display: inline-block; position: relative;
  margin: 12px 10px 4px; padding: 3px 8px 3px 17px;
  color: var(--bd-papertext); background: var(--bd-plate);
  font-size: var(--bd-text-small); font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  box-shadow: 2px 2px 0 var(--bd-shadow);
}
/* The pin: a terracotta head with a glint on its lit shoulder and shade under it — the same
   one-light rule every tile in the scene obeys. */
.staffHead::before, .empty::before {
  content: ""; position: absolute; left: 6px; top: 50%; margin-top: -3px;
  width: 6px; height: 6px; background: var(--bd-terra);
  box-shadow: inset 1px 1px 0 color-mix(in srgb, var(--bd-paper) 55%, transparent),
    inset -1px -1px 0 color-mix(in srgb, var(--bd-ink) 45%, transparent);
}
details.ledger, details.staffBox { margin: 6px 0 0; }
details.ledger > summary, details.staffBox > summary {
  cursor: pointer; padding: 4px 10px; color: ${DIM}; font-size: var(--bd-text-small);
  list-style: none;
}
details.ledger > summary::-webkit-details-marker,
details.staffBox > summary::-webkit-details-marker { display: none; }
/* The disclosure mark is drawn in this board's own pixels — a stepped triangle cut with the same
   clip-path quantisation the world's cast shadows use, never the native marker. Opening gives it
   the quarter turn the world gives a body lying down onto the couch. */
details.ledger > summary::before, details.staffBox > summary.staffHead::after {
  content: ""; display: inline-block; width: 6px; height: 10px;
  background: currentColor;
  clip-path: polygon(0 0, 2px 0, 2px 2px, 4px 2px, 4px 4px, 6px 4px, 6px 6px, 4px 6px,
    4px 8px, 2px 8px, 2px 10px, 0 10px);
  vertical-align: -1px;
}
details.ledger > summary::before { margin-right: 8px; }
details.staffBox > summary.staffHead::after { margin-left: 8px; }
details.ledger[open] > summary::before,
details.staffBox[open] > summary.staffHead::after { transform: rotate(90deg); }
details.ledger > summary:hover { background: var(--bd-hover); color: var(--bd-fg); }
details.staffBox > summary.staffHead:hover {
  background: color-mix(in srgb, var(--bd-deep) 78%, var(--bd-ink));
}
.runs.aged .row, .runs.staff .row { opacity: .85; }
/* A report is indented under the lead that sent it, so the roster reads as a company rather than
   a flat list — you can see who is driving what. */
.row[data-report] { padding-left: 26px; }
.row[data-report] .mark { opacity: .75; }
/* The orchestrator is MARKED, never pinned to the top: this list sorts by who needs you, and a
   healthy boss must not bury a crashed agent. Amber, because the world crowns the brain in the
   lamp's own --wp-h3 ring. */
.brain {
  margin-left: 6px; padding: 0 4px;
  font-size: var(--bd-text-small); border-radius: 0;
  color: color-mix(in srgb, var(--vscode-charts-yellow, #d7ba7d) 55%, var(--bd-fg));
  border: 1px solid color-mix(in srgb, var(--vscode-charts-yellow, #d7ba7d) 55%, var(--bd-bg));
  background: color-mix(in srgb, var(--vscode-charts-yellow, #d7ba7d) 12%, var(--bd-bg));
}
.note { color: ${DIM}; overflow-wrap: anywhere; }
/* Attention reads by SHAPE first; colour only reinforces it. One accent per row, supplied by the
   shared vocabulary — six near-identical rules here were how the two surfaces drifted apart. */
.mark { color: var(--accent, var(--vscode-foreground)); }
/* Nothing to read is a NOTE pinned to the board — paper stock, ink writing, hung by hand. */
.empty {
  display: inline-block; position: relative;
  margin: 16px 12px; padding: 9px 12px 9px 20px;
  color: color-mix(in srgb, var(--bd-ink) 86%, var(--bd-paper));
  background: color-mix(in srgb, var(--bd-paper) 90%, var(--bd-bg));
  box-shadow: 2px 2px 0 var(--bd-shadow);
  transform: rotate(-1deg);
}
.empty::before { left: 7px; }`;
}

/** The roster itself: header, chips, breadcrumb, rows. No document, no script — a fragment. */
/** "3h ago", "just now" — WHEN is the one fact a professional expects instantly and nothing
 *  showed: durations everywhere, dates nowhere. Relative and terse; the tooltip has nothing more
 *  because the registry stores seconds, not stories. */
function agoOf(startedAt: number | null | undefined): string {
  if (!startedAt) return "";
  const s = Math.max(0, Date.now() / 1000 - startedAt);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** Every collection `railBody` actually draws a row from.
 *
 *  The roster renders from FOUR — the live runs, your own sessions, the history ledger and the
 *  staff fold — and callers kept looking at only the first. The legend disappeared exactly when
 *  rows had numbers to explain, and the panel asked the ranking about one bucket's models, so a
 *  colleague in "on staff" rendered as `no ranking places sonnet` beside five `claude-sonnet-5`
 *  rows showing `38.4 · 24th`: one model, two answers, one screen. Both callers ask HERE now, so
 *  a fifth bucket is a change in one place rather than a defect discovered on pixels.
 */
export function everyRow(rail: Rail): Rail["runs"] {
  return [...rail.runs, ...(rail.yours ?? []), ...(rail.ledger?.runs ?? []), ...(rail.staff ?? [])];
}

/** The distinct models drawn anywhere on the roster — what the ranking must be asked about. */
export function modelsOnScreen(rail: Rail): string[] {
  return [...new Set(everyRow(rail)
    .map((r) => (r.run as { model?: string }).model)
    .filter((m): m is string => !!m))];
}

/** The three shapes the roster can take. ONE row model, three layouts: the view is a property of
 *  the CONTAINER, so a table, a card grid and the grouped list are three CSS regimes over
 *  identical markup rather than three renderers that would drift apart the first time a column
 *  was added. The choice is remembered, so the panel opens the way it was left. */
export type RosterView = "grouped" | "table";

export const ROSTER_VIEWS: readonly { id: RosterView; label: string; hint: string }[] = [
  { id: "grouped", label: "Grouped", hint: "Live work, history and staff in their own sections" },
  { id: "table", label: "Table", hint: "One aligned row per agent — the most per screen" },
];

export function railBody(
  rail: Rail,
  voiceOf: (attention: string) => Voice,
  titleOf: (run: Rail["runs"][number]["run"]) => string,
  roleOf: (run: Rail["runs"][number]["run"]) => { id: string; label: string },
  actionsFor: (run: Rail["runs"][number]["run"]) => AgentAction[] = () => [],
  /** How competent that colleague's model is measured to be: the short form a row has width for,
   *  and the full sentence for its tooltip — which must NAME the model, since a number nobody can
   *  attribute is worse than no number. This is the ask's second half, "trust one agent more than
   *  another", and it only exists as a COMPARISON when two colleagues' numbers sit in one frame.
   *  Handed in already built, because this module stays runtime-import-free for its test loader. */
  scoreOf: (run: Rail["runs"][number]["run"]) => { text: string; title: string } | undefined =
    () => undefined,
  /** What that colleague last ran on, when anything did — so a row with no MEASURE can still say
   *  which of the two reasons applies. */
  modelOf: (run: Rail["runs"][number]["run"]) => string | undefined = () => undefined,
  /** Which of the three layouts to wear. Defaults to the one the roster has always had, so a
   *  caller that never chose keeps exactly what it rendered before. */
  view: RosterView = "grouped",
): string {
  // The roster names the measure ONCE, in the words the board uses. It carried the numbers and
  // never said what they were or that they are one measure among several.
  //
  // Gated on EVERY bucket that renders a row, not just the first. A roster whose colleagues were
  // all in "your sessions" or "on staff" — the ordinary shape of a quiet company — printed its
  // numbers with the legend suppressed, so the one place the source is named at rest was missing
  // exactly when the rows were there to explain.
  const measured = everyRow(rail).some((r) => scoreOf(r.run));
  const legend = measured
    ? `<div class="legend">Artificial Analysis intelligence · one measure, not a verdict</div>` : "";
  const chips = rail.chips
    .map((c) => `<button class="chip" data-command="${esc(c.command)}">${esc(c.label)}</button>`)
    .concat('<button class="chip" data-command="interact.openDashboard" title="Interact settings">⚙ Settings</button>')
    .join("");

  // At team level a row is a COLLEAGUE: the name leads (the same name the world's plaque
  // carries — "visual-critic" here and "Visual QA authority" there was three names for one
  // entity), the errand is the quiet second line. Inside a drill-in a row is a CONVERSATION,
  // so the title leads — you already know who you are visiting.
  const drilled = Boolean(rail.filter);
  const rowHtml = (r: Rail["runs"][number]): string => {
    const voice = voiceOf(r.attention);
    // One of YOUR sessions: titled by the project it is building — the fact you recognise it
    // by — never by a task field discovery does not fill.
    const mine = r.attention === "not-ours";
    const who = mine ? (r.run.project || r.run.name)
      : drilled ? titleOf(r.run) : roleOf(r.run).label;
    const errand = !mine && !drilled && (r.run.task ?? "").trim() ? titleOf(r.run) : "";
    const bill = r.cost ?? r.run.cost_usd;
    const provider = (r.run.provider ?? "").trim();
    const rawModel = (modelOf(r.run) ?? "").trim();
    const model = rawModel && rawModel !== "inherit" ? rawModel : "";
    const effort = ((r.run as { reasoning?: string | null; pending_reasoning?: string | null }).reasoning
      ?? (r.run as { pending_reasoning?: string | null }).pending_reasoning ?? "").trim();
    return `
      <li class="row" data-run="${esc(r.run.run_id)}" data-attention="${esc(r.attention)}"${
        r.depth ? ' data-report="1"' : ""}${r.brain ? ' data-brain="1"' : ""}
        style="--accent: var(${esc(voice.tinted ? voice.accent : "--vscode-descriptionForeground")})">
        <span class="run-identity"><span class="mark">${esc(voice.mark)}</span>
        <span class="who">${esc(who)}${
          (r.tasks ?? 1) > 1 ? `<button class="tasks" data-agent="${esc(roleOf(r.run).id)}" title="Show all ${r.tasks} tasks">×${r.tasks}</button>` : ""}${
          r.brain ? '<span class="brain" title="the agent you asked — it put the others to work">brain</span>' : ""}</span>
        <span class="stamp" title="${esc(voice.phrase)}">${esc(voice.word)}</span></span>
        ${errand ? `<details class="oneliner task-detail" data-fold-key="task:${esc(r.run.run_id)}"><summary title="Expand full task"><span>${esc(errand)}</span><b class="task-disclosure-label">Full task</b></summary><p>${esc(r.run.task ?? "")}</p></details>` : '<span class="oneliner"></span>'}
        <span class="run-facts"><span class="provider" title="${esc(provider ? `Provider ${provider}` : "No provider recorded")}">${esc(provider)}</span>
        <span class="model" title="${esc(model || "No model recorded")}">${esc(model)}</span>
        <span class="effort" title="${esc(effort ? `Reasoning ${effort}` : "No reasoning level recorded")}">${esc(effort)}</span>
        ${(() => {
          const sc = scoreOf(r.run);
          // A row with no measure says so. Rendering NOTHING left 31 colleagues blank while the
          // board wrote a dash for the same fact — two vocabularies for one meaning, and blank is
          // indistinguishable from a number that failed to load.
          if (!sc) {
            // TWO different facts, and one sentence was told for both: a colleague with no model
            // recorded inherits, and the vendor picks at spawn; a colleague that HAS one is simply
            // not on the board. Eleven rows were being explained wrongly.
            // inherit is the SENTINEL for "nothing recorded", not a model name — printed as one
            // it read "no ranking places inherit" on 22 rows. The board routes it correctly; this
            // did not.
            const raw = (modelOf(r.run) ?? "").trim();
            const ran = raw && raw !== "inherit" ? raw : "";
            return `<span class="score none" title="${esc(ran
              ? `no ranking places ${ran}`
              : "nothing recorded — it inherits, so the vendor picks its model at spawn")
            }">—</span>`;
          }
          return `<span class="score" title="${esc(sc.title)}">${esc(sc.text)}</span>`;
        })()}
        <span class="when">${esc(agoOf(r.run.started_at))}</span>
        <span class="cost">${r.attention !== "ready" && bill != null ? `$${bill.toFixed(2)}` : ""}</span></span>
        <span class="acts">${actionsFor(r.run).map((a) =>
          `<button class="act" data-action="${esc(a.id)}" data-command="${esc(a.command)}"` +
          ` title="${esc(a.label)}" aria-label="${esc(a.label)}"><span class="actGlyph">${esc(a.mark)}</span>` +
          `<span class="actText">${esc(a.id)}</span></button>`).join("")}</span>
      </li>`;
  };

  const rows = rail.runs.map(rowHtml).join("");

  // Your own sessions — the work this machine is doing right now, newest first, each a real
  // place to go. Above the ledger: live outranks history.
  const yours = (rail.yours ?? []).length
    ? `<div class="staffHead">Your sessions</div><ul class="runs yours">${
        (rail.yours ?? []).map(rowHtml).join("")}</ul>`
    : "";

  // History is ONE line that opens — never seven rows competing with the living team. The
  // header stops double-counting it too: its "done" is only the recent finishes above.
  const ledger = rail.ledger
    ? (() => {
        const l = rail.ledger;
        const bits: string[] = [];
        if (l.done) bits.push(`${l.done} done`);
        if (l.stopped) bits.push(`${l.stopped} stopped`);
        if (l.failed) bits.push(`${l.failed} failed`);
        if (l.cost > 0) bits.push(`$${l.cost.toFixed(2)}`);
        return `<details class="ledger"><summary>History — ${esc(bits.join(" · "))}</summary>` +
          `<ul class="runs aged">${l.runs.map(rowHtml).join("")}</ul></details>`;
      })()
    : "";

  // The company at rest. Folded: the world beside this column already SHOWS every colleague;
  // the roster names them on demand without spending forty rows on quiet desks.
  const staff = (rail.staff ?? []).length
    ? `<details class="staffBox"><summary class="staffHead">On staff — ${rail.staff!.length}</summary>` +
      `<ul class="runs staff">${rail.staff!.map(rowHtml).join("")}</ul></details>`
    : "";

  const empty = rail.runs.length || (rail.yours ?? []).length || rail.ledger
    || (rail.staff ?? []).length
    ? ""
    : `<p class="empty">Nothing here yet — start someone with <b>+ New</b>.</p>`;
  const hasRows = Boolean(rail.runs.length || (rail.yours ?? []).length
    || rail.ledger?.runs.length || (rail.staff ?? []).length);
  const columnHead = hasRows
    ? `<div class="columnHead" aria-hidden="true"><span></span><span>Agent</span><span>Status</span><span>Task</span><span>Provider</span><span>Model</span><span>Effort</span><span>Measure</span><span>Time</span><span>Cost</span><span>Actions</span></div>`
    : "";

  const chooser = `<div class="views" role="group" aria-label="How to show the roster">${
    ROSTER_VIEWS.map((v) =>
      `<button class="view" data-view="${v.id}" aria-pressed="${v.id === view}"` +
      ` title="${esc(v.hint)}">${esc(v.label)}</button>`).join("")}</div>`;

  // The view class goes on a CONTAINER, not on .rail — .rail is the header block and .runs
  // is its SIBLING, so every .view-table .runs .row rule silently matched nothing. A class that
  // is present but not an ancestor styles exactly zero elements.
  return `<div class="roster view-${view}">
<div class="rail">
  ${legend}
  <div><button class="scope" data-command="interact.agents.workspace"
    title="Show agents from another project">${esc(rail.header.scope || "all workspaces")}</button><span
    class="counts">${esc(headerLine(rail.header))}</span></div>
  <div class="chips">${chips}</div>
  ${chooser}
  <input class="seek" id="seek" type="search" placeholder="Filter agents and tasks"
    aria-label="Filter the roster" />
  ${rail.filter ? `<div class="crumbs"><button class="crumb" data-agent="">‹ Everyone</button>` +
    `<span class="crumbSep">/</span><span class="crumbNow">${esc(rail.filter)}</span></div>` : ""}
</div>
${empty}
${columnHead}
<ul class="runs">${rows}</ul>
${yours}${ledger}${staff}</div>`;
}

/** What the roster's buttons DO. Shared by both hosts for the same reason as the style. */
export function railScript(): string {
  return `const api = (window.__wpApi ||
    (typeof acquireVsCodeApi === "function" ? acquireVsCodeApi() : null));
  function bindRail() {
  document.querySelectorAll(".chip, .scope").forEach((b) => {
    b.addEventListener("click", () => api.postMessage({ type: "command", command: b.dataset.command }));
  });
  document.querySelectorAll(".row").forEach((r) => {
    r.addEventListener("click", () => api.postMessage({ type: "open", runId: r.dataset.run }));
  });
  document.querySelectorAll(".task-detail").forEach((detail) => {
    detail.addEventListener("click", (event) => event.stopPropagation());
  });
  {
    // The filter a professional reaches for before scrolling. Client-side, over what the row SAYS,
    // because that is what the person is matching on too.
    const seek = document.getElementById("seek");
    // The reader's own fold state, remembered while a search is on — held on the window, not in
    // this closure: a repaint during the search re-binds everything, and a closure-held snapshot
    // died with it, so the search's forced-open folds were "restored" on clear.
    const fold = (window.__railFold = window.__railFold || { folded: null });
    if (seek) seek.addEventListener("input", () => {
      const q = seek.value.trim().toLowerCase();
      document.querySelectorAll(".row").forEach((row) => {
        row.style.display = !q || row.textContent.toLowerCase().includes(q) ? "" : "none";
      });
      // A match buried in a CLOSED fold is a silent miss — searching opens the folds. Clearing
      // the filter gives the reader back what THEY had open: re-folding to a "resting state"
      // folded an expanded ON STAFF shut on every clear.
      const details = Array.from(document.querySelectorAll("details"));
      if (q) {
        if (fold.folded === null) fold.folded = details.map((d) => d.open);
        details.forEach((d) => { d.open = true; });
      } else if (fold.folded !== null) {
        details.forEach((d, i) => { d.open = fold.folded[i] ?? d.open; });
        fold.folded = null;
      }
    });
  }
  document.querySelectorAll(".tasks, .crumb").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();  // going to the agent must not also open the conversation under it
      api.postMessage({ type: "agent", id: b.dataset.agent });
    });
  });
  document.querySelectorAll(".views .view").forEach((b) => {
    b.addEventListener("click", () => {
      api.postMessage({ type: "rosterView", view: b.dataset.view });
    });
  });
  document.querySelectorAll(".act").forEach((b) => {
    b.addEventListener("click", (e) => {
      // Never let an action also open the row underneath it.
      e.stopPropagation();
      api.postMessage({ type: "act", command: b.dataset.command, runId: b.closest(".row").dataset.run });
    });
  });  }
  bindRail();
  // The panel MOUNTS once and is updated by message thereafter — rebuilding the document would
  // kill every running animation in the room next door. So the roster is replaced in place and
  // re-bound, exactly as the room is.
  function applyRoster(host, html) {
    // The swap is wholesale, so remember what the reader had opened, typed and scrolled: every
    // repaint (a model picked, a run finishing) used to fold ON STAFF and empty the filter —
    // applying one profile to N agents cost a re-expand per agent.
    var open = {};
    host.querySelectorAll("details[class]").forEach(function (d) { open[d.dataset.foldKey || d.className] = d.open; });
    var seek = host.querySelector("#seek"), typed = seek ? seek.value : "", top = host.scrollTop;
    // ...and WHERE THE KEYBOARD WAS. The swap disconnects the focused row, so a reader tabbing
    // through the roster lost focus every time the list repainted — four times a second — and Tab
    // never reached a row at all. Remember the run, restore the focus onto its new element.
    var act = document.activeElement;
    var focused = act && act.closest ? act.closest("li.row") : null;
    var focusedRun = focused ? focused.getAttribute("data-run") : null;
    host.innerHTML = html;
    if (focusedRun) {
      var again = host.querySelector('li.row[data-run="' + focusedRun + '"]');
      if (again) again.focus();
    }
    host.querySelectorAll("details[class]").forEach(function (d) { const key = d.dataset.foldKey || d.className; if (key in open) d.open = open[key]; });
    bindRail();
    var seek2 = host.querySelector("#seek");
    if (seek2 && typed) { seek2.value = typed; seek2.dispatchEvent(new Event("input")); }
    host.scrollTop = top;
  }
  window.addEventListener("message", function (e) {
    var m = e && e.data;
    if (!m || m.type !== "roster" || typeof m.html !== "string") return;
    var host = document.querySelector(".wp-list");
    if (!host) return;
    // A wholesale swap destroys whatever the keyboard is standing on. Putting focus back on a row
    // rescues focus already ON one — it cannot rescue focus trying to ENTER, which is why twelve
    // trusted Tabs never reached a row: each Tab stepped in, the repaint dropped focus to <body>,
    // and the next Tab started again from the top control. So while the reader's keyboard is in
    // the list, the repaint WAITS. Nothing is lost or stale-forever — the newest roster is held
    // and applied the moment focus leaves, which is also the moment nobody is reading a row.
    // Only defer for a reader who is actually THERE. Armed while the window was already blurred,
    // the "blur" backstop could never fire again — and row-to-row movement returns early from
    // focusout — so the roster stayed frozen for as long as the reader worked the list.
    // Only while the keyboard is on a ROW — which is the whole reason the deferral exists. Testing
    // "anywhere inside the list" also caught the list's own CONTROLS: clicking a view button
    // focused it, and the button lives here, so the click deferred the repaint it had just asked
    // for. Measured at 30.2 s to flip, released only by focusing away or the timeout, which is why
    // it read as intermittent — any unrelated click flushed the queue.
    var act = document.activeElement;
    var inside = document.hasFocus() && act && act !== document.body
      && act.closest && act.closest("li.row") && host.contains(act);
    if (inside) {
      window.__railPending = m.html;
      if (!window.__railWaiting) {
        window.__railWaiting = true;
        var flush = function () {
          host.removeEventListener("focusout", onOut);
          window.removeEventListener("blur", flush);
          clearTimeout(window.__railCap);
          window.__railWaiting = false;
          var held = window.__railPending;
          window.__railPending = null;
          // Re-query: a full re-render can detach the node this closure captured, and applying the
          // held roster into a dead element loses it silently.
          var live = document.querySelector(".wp-list") || host;
          if (held) applyRoster(live, held);
        };
        var onOut = function (ev) {
          if (ev.relatedTarget && host.contains(ev.relatedTarget)) return;  // moved row to row
          flush();
        };
        host.addEventListener("focusout", onOut);
        // Switching to another window leaves activeElement parked on a row: focusout never fires,
        // and the roster would sit frozen for as long as the editor is in the background. Nobody
        // is reading it then, so that is exactly the moment to catch up.
        window.addEventListener("blur", flush);
        // A backstop, because every other release depends on an event that may not come: nobody
        // reads one row for half a minute, and a roster frozen that long is its own defect.
        window.__railCap = setTimeout(flush, 30000);
      }
      return;
    }
    applyRoster(host, m.html);
  });
`;
}

export function railDocument(nonce: string, style: string, body: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
${style}
</style>
</head>
<body>
${body}
<script nonce="${nonce}">
${railScript()}
</script>
</body>
</html>`;
}
