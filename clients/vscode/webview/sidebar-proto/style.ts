/** The look of the board.
 *
 *  Three rules run through all of it, and two of them are the workplace's own — deliberately, since
 *  the whole point of this panel is that it is the same world seen from a different desk.
 *
 *  COLOUR follows the editor. Paper, ink, steel and the printed rules are mixed from `--vscode-*`
 *  tokens, so the board is a warm dark card in a dark theme and a cream one in a light theme with
 *  no second palette existing anywhere. Only the people keep literal tones, which is why they are
 *  the one thing imported from the workplace unchanged.
 *
 *  MOTION follows one beat. Every ambient duration is an integer ratio of `--beat`, so a board full
 *  of live work moves as ONE thing rather than as six independent loops. The easing is declared
 *  inside the keyframes as well as on the shorthand, because `getComputedTiming().easing` reports
 *  `linear` for every CSS animation and the real value only survives on the keyframe.
 *
 *  PAPER IS A SILHOUETTE, NOT A BRIGHTNESS. The temptation with a paper metaphor is to make the
 *  slips white, and twelve white cards in a dark side bar is glare, not craft. So what makes these
 *  read as paper is the die-cut: the punch hole with a steel rod behind it, the binding gutter, the
 *  perforation between parts of the form, the tear on a job that died, the curled corner on one
 *  nobody has been back to. The fill can then follow the theme all the way down, which it does.
 *  Same reasoning as the workplace's derived ink rim — the shape carries the reading, so the colour
 *  is free to follow the editor.
 */

import { STAMP_CSS } from "../workplace/status";

export const STYLE = String.raw`
*, *::before, *::after { box-sizing: border-box; }
/* Not a reset — the two UA margins that actually bite. <figure>/<p>/<h3> all carry one, and a
   silent 1em 40px on a slip is how every row ends up mysteriously too wide. */
html, body, p, h1, h2, h3, header, footer, section, article, figure { margin: 0; padding: 0; }

body {
  font-family: var(--vscode-font-family, system-ui, sans-serif);
  color: var(--vscode-foreground, #ccc);
  background: var(--vscode-editor-background, #1e1e1e);
  font-size: 12px;
  -webkit-font-smoothing: antialiased;
}

/* The size container. The narrow rules below have to restyle .sp itself, and a container query
   cannot style its own container — hence a wrapper whose only job is to be measured. Querying the
   CONTAINER rather than the viewport is also the truthful test: a side bar's width is its own, not
   the window's. */
.sp-panel { container-type: inline-size; }

.sp {
  /* ── tempo ───────────────────────────────────────────────────────────────────────────────
     One beat, shared with the workplace so the two surfaces breathe together. */
  --beat: 2.4s;
  --gait: calc(var(--beat) / 2);
  --ease-settle: cubic-bezier(.22, 1, .32, 1);

  /* ── the world, mixed from the theme ─────────────────────────────────────────────────── */
  --wp-bg: var(--vscode-editor-background, #1e1e1e);
  --wp-fg: var(--vscode-editor-foreground, #d4d4d4);
  --wp-dim: var(--vscode-descriptionForeground, #9a9a9a);
  --wp-ink: #14101c;
  --wp-tint: #e8e4f0;

  /* The same seven pod hues the building uses, from the theme's own chart palette. None of them
     is red: red means "stuck" everywhere on this surface, and a team whose spine happened to be
     red would read as a team on fire. */
  --wp-h1: var(--vscode-charts-blue, #4f9cf5);
  --wp-h2: var(--vscode-charts-purple, #b180d7);
  --wp-h3: var(--vscode-charts-yellow, #e2c08d);
  --wp-h4: var(--vscode-charts-green, #6fc28b);
  --wp-h5: var(--vscode-charts-orange, #e8925a);
  --wp-h6: color-mix(in srgb, var(--wp-h1) 55%, var(--wp-h4));
  --wp-h7: color-mix(in srgb, var(--wp-h2) 62%, var(--vscode-charts-red, #e06c75));

  --wp-ok: var(--vscode-charts-green, #6fc28b);
  --wp-bad: var(--vscode-errorForeground, #e06c75);

  /* ── stock and steel ─────────────────────────────────────────────────────────────────────
     A warm manila lifted just off the background — enough that a slip is an object, not so much
     that eight of them glare. The silhouette is doing the "this is paper" work, not the value. */
  --sp-paper: color-mix(in srgb, #d9cfae 9%, var(--wp-bg));
  --sp-paper-shade: color-mix(in srgb, #d9cfae 8%, var(--wp-bg));
  /* The counterfoil's own stock. It was on paper-shade, which is one percent off the paper and
     measured two levels apart — on a light theme that is enough to see the stub as a separate part
     of the form, on a dark one it was not, and the strip was reading as more card. Its own token,
     stepped until the difference is visible on the dark board too. */
  --sp-stub: color-mix(in srgb, #d9cfae 6%, var(--wp-bg));
  --sp-paper-back: color-mix(in srgb, var(--wp-fg) 26%, var(--wp-bg));
  --sp-edge: color-mix(in srgb, var(--wp-fg) 30%, var(--wp-bg));
  --sp-shadow: color-mix(in srgb, var(--wp-ink) 62%, transparent);
  --sp-rule: color-mix(in srgb, var(--wp-fg) 22%, var(--wp-bg));
  --sp-ink: var(--wp-fg);
  --sp-dim: color-mix(in srgb, var(--wp-dim) 20%, var(--wp-fg));
  --sp-plate: color-mix(in srgb, var(--wp-fg) 82%, var(--wp-bg));

  --sp-rod: color-mix(in srgb, var(--wp-fg) 52%, var(--wp-bg));
  --sp-rod-hi: color-mix(in srgb, var(--wp-fg) 78%, var(--wp-bg));
  --sp-rod-lo: color-mix(in srgb, var(--wp-fg) 26%, var(--wp-bg));

  /* ── the desk ────────────────────────────────────────────────────────────────────────────
     The panel is not a canvas the paper floats on, it is a SURFACE the paper is lying on. A warm
     board, a little darker than the editor so the stock sits on top of it rather than in it, with
     the boards running across the column so they read against the one vertical thing here — the
     rod. This is the same decision the building makes with its floor: a plane you can see, so a
     part of it with nobody on it is an empty ROOM and not a hole in the picture. */
  --sp-desk: color-mix(in srgb, #3d3025 26%, var(--wp-bg));
  --sp-desk-line: color-mix(in srgb, var(--wp-ink) 44%, transparent);
  --sp-desk-far: color-mix(in srgb, var(--wp-ink) 34%, transparent);
  --sp-desk-rim: color-mix(in srgb, var(--wp-ink) 55%, transparent);
  /* The lamp over the desk. Literally the workplace's lit-room recipe, pointed down at the top of
     the column where the live work sits, so the panel has the same warm/cool fall the building
     has instead of one flat value everywhere. */
  --sp-lamp: color-mix(in srgb, #ffd9a0 9%, transparent);

  /* Three tones for anything standing on the desk, in the rod's own proportions. */
  /* The pad is a step AWAY from the desk in whichever direction is not toward the paper — lighter
     on a dark theme, darker on a light one — so it never competes with the stock for the eye. */
  --sp-pad: color-mix(in srgb, var(--wp-fg) 7%, var(--sp-desk));
  /* Printed ON the pad, so it is mixed against the PAD and not against the editor background the
     card rules are mixed against — a rule tuned for paper is a ghost on felt. */
  --sp-pad-rule: color-mix(in srgb, var(--wp-fg) 30%, var(--sp-pad));
  --sp-prop: color-mix(in srgb, var(--wp-fg) 30%, var(--sp-desk));
  --sp-prop-hi: color-mix(in srgb, var(--wp-fg) 48%, var(--sp-desk));
  --sp-prop-lo: color-mix(in srgb, var(--wp-fg) 14%, var(--sp-desk));

  /* ── stock, up close ─────────────────────────────────────────────────────────────────────
     The tooth of the paper — the thing that makes a card stop being a filled rectangle. The job is
     that you register a SURFACE, never that you notice lines.
     Three decisions, two of them learned by getting it wrong first. The fibres run DIAGONALLY,
     because an upright grid of hairlines is graph paper and it lands parallel to the two loudest
     lines on this board — the perforations across and the rod down — so it fights them. The two
     pitches are COPRIME (5 and 7), so the interference never settles into a repeat anyone can name.
     And the amplitude is MEASURED, not eyeballed: a handful of levels peak to peak on the paper,
     which is felt and not seen. Measured on an isolated swatch, not guessed off a card with text
     on it: 26% came out as crosshatch, 12% still read as a weave, this is about four levels. */
  --sp-tooth: color-mix(in srgb, var(--wp-ink) 5%, transparent);
  --sp-laid: color-mix(in srgb, var(--wp-fg) 2%, transparent);
  /* Both fibres darken the stock, so the grain lowers its MEAN value — and every contrast figure
     on this panel was measured against the stock without it. Left uncompensated that is a real,
     if small, quieting of every word on a card, arrived at by a decision that was supposed to be
     about texture only. So the grain carries its own flat lift underneath, sized to put the mean
     back exactly where it was; on a dark theme one fibre already lights and the sum comes out
     positive, so there is nothing to give back. */
  --sp-grain-lift: transparent;
  --sp-proj-mix: 70%;
  /* Chart hues are tuned to sit on the editor background, not on paper: DONE and ERROR measured
     3.6:1 peak against the slip. Mixed toward the ink, which flips with the theme, so the same
     ratio brightens on dark stock and darkens on light while the hue still reads. */
  /* The shared stamp, mounted on PAPER. The device comes from workplace/status.ts and is the same
     element the building prints over a worker's head; what this panel supplies is only the
     physics of ink pressed into a sheet — no plate behind it, no shadow under it, and the mix
     partner is the paper's own ink rather than the editor foreground. */
  --stamp-ink: var(--sp-ink);
  --stamp-quiet: var(--sp-dim);
  --stamp-mix: 52%;
  --sp-grain:
    repeating-linear-gradient(45deg, var(--sp-tooth) 0 1px, transparent 1px 5px),
    repeating-linear-gradient(-45deg, var(--sp-laid) 0 1px, transparent 1px 7px),
    linear-gradient(var(--sp-grain-lift), var(--sp-grain-lift));
  --sp-pad-nap: repeating-linear-gradient(-45deg, var(--sp-laid) 0 1px, transparent 1px 7px);
  /* A punched perforation, square because everything else on this board is. */
  --sp-perf: color-mix(in srgb, var(--wp-ink) 62%, transparent);
  /* Three hard stops in the proportions of the tip's own pixel row (l l m m m d), so the drawn
     point and the stretched rod are the same cylinder and the join is invisible. */
  --sp-rod-fill: linear-gradient(90deg,
    var(--sp-rod-hi) 0 33.333%,
    var(--sp-rod) 33.333% 83.333%,
    var(--sp-rod-lo) 83.333% 100%);

  position: relative;
  display: flex;
  flex-direction: column;
  padding: 9px 9px 15px 4px;
  min-height: 100vh;
  /* Lamp first, then the boards, then the board colour. */
  background:
    radial-gradient(150% 46% at 50% -6%, var(--sp-lamp) 0%, transparent 72%),
    /* The far end of the desk. The lamp only reaches the top third, and below it the board was ONE
       value for the rest of the panel — a texture with no fall, which reads flatter than it
       measures. A few levels of falloff to the bottom is what turns a tiled pattern into a plane
       going away from you. */
    linear-gradient(180deg, transparent 34%, var(--sp-desk-far) 100%),
    repeating-linear-gradient(180deg,
      transparent 0 17px,
      var(--sp-desk-line) 17px 18px),
    var(--sp-desk);
}

/* A light theme needs the ink to soften and the paper to go the other way, or the slips end up
   darker than the page they sit on and the whole pile reads as a hole. */
.vscode-light .sp,
.vscode-high-contrast-light .sp {
  --wp-ink: #2c2438;
  /* The tint mixes INTO the shirt colour, so on a light theme it has to go the other way or every
     pod hue washes out to a pastel with nothing to sit against. The workplace flips it here too. */
  --wp-tint: #2f2a3a;
  --sp-paper: color-mix(in srgb, #efe6cd 88%, var(--wp-bg));
  --sp-paper-shade: color-mix(in srgb, #e3d8ba 88%, var(--wp-bg));
  --sp-stub: color-mix(in srgb, #e3d8ba 88%, var(--wp-bg));
  --sp-paper-back: color-mix(in srgb, #b9ac8a 92%, var(--wp-bg));
  --sp-edge: color-mix(in srgb, var(--wp-fg) 42%, var(--wp-bg));
  --sp-shadow: color-mix(in srgb, var(--wp-ink) 30%, transparent);
  --sp-rule: color-mix(in srgb, var(--wp-fg) 30%, var(--wp-bg));
  --sp-ink: var(--wp-ink);
  --sp-dim: color-mix(in srgb, var(--wp-ink) 90%, var(--wp-bg));
  --sp-rod: color-mix(in srgb, var(--wp-fg) 46%, var(--wp-bg));
  --sp-rod-hi: color-mix(in srgb, var(--wp-fg) 24%, var(--wp-bg));
  --sp-rod-lo: color-mix(in srgb, var(--wp-fg) 72%, var(--wp-bg));

  /* Light oak rather than the walnut, and the ordering the building had to learn: the desk goes
     DARKER than the stock here, or the empty stretch comes out the brightest thing on the panel
     and the eye is pulled to the part with nothing on it. Paper is the brightest surface in both
     themes, which is the only rule that matters. */
  --sp-desk: color-mix(in srgb, #b98d55 45%, var(--wp-bg));
  /* The board seams are mixed against the DESK, and the desk is 170 levels brighter here than on
     the dark theme, so the same alpha buys about four times the line. 15% measured 26 levels of
     step and started reading as ruled paper rather than as a seam between two boards. */
  --sp-desk-line: color-mix(in srgb, var(--wp-ink) 8%, transparent);
  --sp-desk-far: color-mix(in srgb, var(--wp-ink) 9%, transparent);
  --sp-desk-rim: color-mix(in srgb, var(--wp-ink) 40%, transparent);
  --sp-lamp: color-mix(in srgb, #fff3dc 46%, transparent);
  --sp-pad: color-mix(in srgb, var(--wp-ink) 9%, var(--sp-desk));
  --sp-pad-rule: color-mix(in srgb, var(--wp-ink) 38%, var(--sp-pad));
  /* Both fibres go dark on cream: a lighter-than-paper line is invisible when the paper is
     already the brightest thing, so the tooth is all there is to see by. */
  --sp-laid: color-mix(in srgb, var(--wp-ink) 1.5%, transparent);
  --sp-tooth: color-mix(in srgb, var(--wp-ink) 2%, transparent);
  --sp-perf: color-mix(in srgb, var(--wp-ink) 42%, transparent);
  --sp-grain-lift: color-mix(in srgb, #ffffff 6%, transparent);
  --sp-proj-mix: 52%;
  --stamp-mix: 40%;
}

/* ── the spike ───────────────────────────────────────────────────────────────────────────────
   One rod, from above the first slip to below the composer. It is the reason this panel is one
   object: the paperwork and the thing you type into are impaled on the same piece of steel. */

.sp-rail {
  position: absolute;
  left: 10px; top: 0; bottom: 0;
  width: 12px;
  z-index: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  pointer-events: none;
}
.sp-tip svg, .sp-base svg { display: block; }
.sp-rod {
  flex: 1 1 auto;
  width: 12px;
  background: var(--sp-rod-fill);
  /* The rod throws a shadow onto the desk. It is drawn on the rail, which sits UNDER the paper, so
     it only ever appears on the bare stretches — the steel is lit where you can see the wood and
     buried where you cannot, which is what makes the pile read as sitting ON something. */
  box-shadow: 5px 0 7px -3px var(--sp-shadow);
}
.sp-base { margin-left: -4px; }

/* ── the plate by the door ─────────────────────────────────────────────────────────────────
   The same pressed sign the workplace hangs over its entrance: light plate, background-coloured
   letters, hard ink shadow. Copied on purpose — it is the cheapest possible proof that the two
   surfaces are one product. */

.sp-plate {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  margin: 0 0 8px 22px;
}
.sp-plate-name {
  padding: 2px 8px;
  background: var(--sp-plate);
  color: var(--wp-bg);
  border: 1px solid var(--sp-edge);
  box-shadow: 2px 2px 0 0 var(--sp-shadow);
  font-weight: 700;
  font-size: 10px;
  letter-spacing: .18em;
  text-transform: uppercase;
}
.sp-tally { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.sp-chip {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 10px; color: var(--sp-dim);
  --mark: var(--sp-dim);
}
.sp-chip b { color: var(--sp-ink); font-weight: 600; font-variant-numeric: tabular-nums; }
.sp-chip:first-child { --mark: var(--wp-ok); }
.sp-chip svg { display: block; }
.sp-chip-sum b { font-variant-numeric: tabular-nums; }

/* ── one work order ────────────────────────────────────────────────────────────────────────
   A team is ONE piece of paper: the lead printed at the top, its reports stapled underneath, one
   punch hole for the document. The 22px gutter on the left is the binding margin — it holds the
   hole and the staples, and it is what stops the text colliding with the rod. */

.sp-stack { position: relative; z-index: 1; }

.sp-order {
  position: relative;
  margin: 0 0 12px 0;
  padding: 5px 7px 0 22px;
  /* Stock, not fill. The tooth runs across the whole body of the card rather than being confined to
     the accents at its edges — that difference is the whole gap between "a printed form" and "a
     rectangle with some paper decoration bolted to it". */
  background: var(--sp-grain), var(--sp-paper);
  border: 1px solid var(--sp-edge);
}

/* A work order is a MULTI-PART form: what you are reading is the top copy, and the sheet under it
   shows along two edges. Cheaper and truer than a drop shadow — a shadow says "this floats", an
   offset second sheet says "there is more of this document underneath". */
.sp-order::after {
  content: "";
  position: absolute;
  left: 3px; right: -3px; top: 3px; bottom: -3px;
  z-index: -1;
  background: var(--sp-paper-shade);
  border: 1px solid var(--sp-edge);
  box-shadow: 2px 2px 0 0 var(--sp-shadow);
  pointer-events: none;
}

/* The margin rule. Every printed form has one down its binding edge, and it is most of why a
   rectangle of theme colour reads as a FORM rather than as a card. */
.sp-order::before {
  content: "";
  position: absolute;
  left: 19px; top: 0; bottom: 0;
  width: 1px;
  background: color-mix(in srgb, var(--wp-fg) 20%, transparent);
  pointer-events: none;
}

/* The team's colour, run down the binding edge. A report standing three rooms away in the
   workplace keeps this same hue on its shirt — one assignment, two surfaces. */
.sp-spine {
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 4px;
  background: var(--accent, var(--wp-h1));
}

/* Punched, with the rod visible through it. The fill is the rod's own gradient at the rod's own
   x-offset, so it is pixel-identical to seeing the steel through the hole. */
.sp-hole {
  position: absolute;
  left: 6px; top: 7px;
  width: 12px; height: 12px;
  /* Round, because a punched hole is — the one curve in a panel of hard edges, earning the
     exception: a 12px square filled with the same gradient read as a BUTTON, not as steel seen
     through paper. */
  border-radius: 50%;
  background: var(--sp-rod-fill);
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--wp-ink) 80%, transparent),
    inset 0 2px 3px -1px color-mix(in srgb, var(--wp-ink) 85%, transparent),
    0 1px 0 0 color-mix(in srgb, var(--wp-fg) 22%, transparent);
}

/* ── one job ──────────────────────────────────────────────────────────────────────────────── */

.sp-slip {
  position: relative;
  padding: 6px 0 6px;
  cursor: pointer;
  transition: transform 120ms var(--ease-settle), filter 120ms linear;
}
/* The parts of the form are PERFORATED apart, not ruled apart. Square punches, because every other
   mark on this board is square, and they run out past the text on both sides — through the binding
   margin and out to the card's edge — because that is where a real perforation goes: across the
   whole sheet, not just under the words. */
.sp-slip::before {
  content: "";
  position: absolute;
  left: -16px; right: -7px; top: 0;
  height: 2px;
  background: repeating-linear-gradient(90deg, var(--sp-perf) 0 2px, transparent 2px 5px);
  pointer-events: none;
}
.sp-slip:first-of-type::before { display: none; }
.sp-slip[data-depth="1"] { margin-left: 9px; }

.sp-slip:hover { transform: translate(-1px, -1px); filter: brightness(1.07); }
.sp-slip:focus-visible {
  outline: 2px solid var(--vscode-focusBorder, #0078d4);
  outline-offset: -1px;
}

.sp-row { display: flex; gap: 7px; align-items: flex-start; }
.sp-mug { flex: 0 0 auto; line-height: 0; }
.sp-mug-slim { padding-top: 3px; --mark: var(--sp-dim); }
.sp-slip[data-status="running"] .sp-mug-slim { --mark: var(--wp-ok); }
.sp-slip[data-status="error"] .sp-mug-slim { --mark: var(--wp-bad); }
.sp-body { flex: 1 1 auto; min-width: 0; }

.sp-head { display: flex; align-items: center; gap: 5px; }
.sp-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12px;
  font-weight: 700;
  line-height: 1.25;
  color: var(--sp-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sp-head > :not(.sp-name) { flex: 0 0 auto; }

.sp-role {
  /* 11px at 600, not 10px at 400: contrast on text this small is decided by how much of each
     glyph is actually the ink colour rather than an anti-aliased blend, so weight and size move
     the measured number as much as the colour does. */
  font-size: 11px;
  font-weight: 600;
  /* Not plain --sp-dim. Measured over glyph COVERAGE rather than at the glyph core, this line came
     in at 4.01:1 on cream stock against an 8.35 nominal — a two-thirds loss to anti-aliasing, and
     under the floor. Pulled a little toward the ink; it stays clearly the quieter of the two lines
     because the name above it is at full ink and this is not. */
  color: color-mix(in srgb, var(--sp-dim) 55%, var(--sp-ink));
  letter-spacing: .02em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* What it is actually doing, in its own words. Two lines, then it stops — the whole sentence is on
   the hover title and in the conversation a click away. */
.sp-said {
  margin-top: 2px;
  font-size: 11px;
  line-height: 1.34;
  color: var(--sp-ink);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.sp-said-none { color: var(--sp-dim); font-style: italic; }

.sp-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 3px;
  font-size: 10px;
  color: var(--sp-dim);
  font-variant-numeric: tabular-nums;
}
.sp-from { margin-left: auto; color: var(--sp-dim); }

/* ── the pile ──────────────────────────────────────────────────────────────────────────────
   Paper sinks. Live work sits on top at full height; anything finished, held or somebody else's
   is pressed down to a sliver you can still read a name off. Vertical space goes to whatever wants
   a person's attention, which is the one thing a list of equal rows cannot do. Lifting a slip —
   hover or keyboard focus — brings it back up. */

.sp-slip[data-fold="slim"] { padding: 3px 0 4px; }
.sp-slip[data-fold="slim"] .sp-body { display: flex; align-items: center; gap: 7px; }
.sp-slip[data-fold="slim"] .sp-head { flex: 1 1 auto; min-width: 0; }
.sp-slip[data-fold="slim"] .sp-name { font-size: 11px; font-weight: 600; color: var(--sp-dim); }
.sp-slip[data-fold="slim"] .sp-role,
.sp-slip[data-fold="slim"] .sp-said { display: none; }
.sp-slip[data-fold="slim"] .sp-foot { flex: 0 0 auto; margin-top: 0; }
.sp-slip[data-fold="slim"] .sp-from { display: none; }
.sp-slip[data-fold="slim"] .wp-stamp { transform: rotate(-4deg); }

.sp-slip[data-fold="slim"]:hover .sp-body,
.sp-slip[data-fold="slim"]:focus-visible .sp-body { display: block; }
.sp-slip[data-fold="slim"]:hover .sp-name,
.sp-slip[data-fold="slim"]:focus-visible .sp-name { color: var(--sp-ink); }
.sp-slip[data-fold="slim"]:hover .sp-role,
.sp-slip[data-fold="slim"]:focus-visible .sp-role { display: block; }
.sp-slip[data-fold="slim"]:hover .sp-said,
.sp-slip[data-fold="slim"]:focus-visible .sp-said { display: -webkit-box; }
.sp-slip[data-fold="slim"]:hover .sp-foot,
.sp-slip[data-fold="slim"]:focus-visible .sp-foot { margin-top: 3px; }

/* ── the marks ────────────────────────────────────────────────────────────────
   Status is read by SHAPE first — a filled disc, a tick, a pointed wedge, an open square, an
   hourglass — and by colour second, never colour alone. Running work is UNSTAMPED: an open job
   with nothing stamped on it is the oldest "in progress" signal there is, and it saves the loud
   marks for what wants looking at.

   The stamp rule is NOT written here. It is imported from the workplace, byte for byte, because
   this device is the whole reason the two panels now say the same fact the same way — and because
   two hand-kept copies is exactly how the pod colours drifted the last time. */
${STAMP_CSS}
.sp-lamp { --mark: var(--wp-ok); line-height: 0; }
.sp-lamp svg { display: block; animation: sp-pulse calc(var(--beat) / 3) ease-in-out infinite; }

.sp-out { line-height: 0; --pod: var(--accent, var(--wp-h1)); }
.sp-out svg { display: block; animation: sp-flag var(--beat) ease-in-out infinite; transform-origin: 0 50%; }

.sp-snooze { line-height: 0; --mark: var(--sp-dim); }
.sp-snooze svg { display: block; }

/* The curl a slip takes when nobody has been back to it. Sits on the outside corner of the paper,
   so it reads as the sheet lifting rather than as an icon printed on it. */
.sp-ear { position: absolute; right: 0; bottom: 0; line-height: 0; pointer-events: none; }
/* A buried slip has no room to curl — its sleep mark already carries the reading, and an ear here
   lands straight on top of the cost. Lift it and the room comes back, so the ear does too: as the
   fixture stood, every stalled slip was slim, which meant the curl was drawn on every one of them
   and visible on none. */
.sp-slip[data-fold="slim"] .sp-ear { display: none; }
.sp-slip[data-fold="slim"]:hover .sp-ear,
.sp-slip[data-fold="slim"]:focus-visible .sp-ear { display: block; }
.sp-ear svg { display: block; }

/* The staple that holds a report onto its lead. Lives in the binding gutter, which is exactly
   where a real one would be. */
.sp-staple { position: absolute; left: -13px; top: 8px; line-height: 0; }
/* A staple is bent metal lying ON the paper, so it throws a hard one-pixel shadow. Without it the
   thing is a printed mark; with it there is something physically pressed through the sheet. */
.sp-staple svg { display: block; filter: drop-shadow(1px 1px 0 var(--sp-shadow)); }

/* A job that died is TORN. The tear is generated so the zigzag stays even at any width, and it is
   the status you read before any colour or any word. */
.sp-slip[data-torn="1"] { padding-bottom: 12px; }
/* The tear paints the PANEL, not the paper: the notches are where the sheet is missing, so what
   shows through them is whatever is behind the work order. Clipped to a generated sawtooth. */
.sp-tear {
  position: absolute;
  left: -18px; right: -7px; bottom: 0;
  height: 8px;
  background: var(--wp-bg);
}
.sp-slip[data-torn="1"] { background: color-mix(in srgb, var(--wp-bad) 5%, transparent); }

/* Somebody else's session is flat and unowned: no face, no colour, dashed. */
/* "Somebody else's session" used to be said with an opacity of .8 on the whole slip, which dims the
   TEXT along with the paper — the same mistake as the stamp above and as the workplace nameplate
   before it. Opacity is never how a surface says "quieter" when it has words on it. Said with the
   paper and the edge instead, so the ink stays at full strength. */
.sp-slip[data-status="foreign"] {
  --sp-paper: var(--sp-paper-shade);
  border-style: dashed;
}

/* ── the form's total ──────────────────────────────────────────────────────────────────────── */

/* The foot of the form is a COUNTERFOIL — the stub you keep, perforated off the body of the order
   and printed on the same stock a shade down. It bleeds to the card's edges (the negative margins
   cancel the order's padding), so the paper language covers the full width of the sheet instead of
   stopping where the text does. */
.sp-total {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px -7px 0 -22px;
  padding: 4px 7px 4px 22px;
  background: var(--sp-grain), var(--sp-stub);
  font-size: 9px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--sp-dim);
}
.sp-total::before {
  content: "";
  position: absolute;
  left: 0; right: 0; top: 0;
  height: 2px;
  background: repeating-linear-gradient(90deg, var(--sp-perf) 0 2px, transparent 2px 5px);
  pointer-events: none;
}
/* The pod hue, pulled toward the ink until it is READABLE at nine pixels. A chart colour is chosen
   to be distinguishable from six other chart colours, never to be legible as small bold type on a
   card, and the darkest of the seven measured 4.03:1 in the dark theme — under the floor for text
   this size. The mix keeps enough of the hue to tie the strip to its spine and its shirts, and the
   RATIO is per theme: a light theme's chart palette is mid-valued against near-white stock, so the
   same 70% that clears the floor on a dark card leaves the yellow at 4.13:1 on a cream one. */
.sp-proj { color: color-mix(in srgb, var(--accent, var(--wp-h1)) var(--sp-proj-mix), var(--sp-ink)); font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sp-sum { margin-left: auto; font-variant-numeric: tabular-nums; letter-spacing: 0; }
/* A lone worker keeps only the project stamp — the strip is still there so the sheet has a foot,
   but it stops repeating a figure that is already one line above it. */
.sp-total-solo { padding: 2px 0 3px; }

.sp-empty {
  position: relative;
  z-index: 1;
  margin-left: 22px;
  padding: 14px 0 6px;
  color: var(--sp-dim);
  font-style: italic;
}

/* ── the clear desk ────────────────────────────────────────────────────────────────────────
   Everything between the last order and the composer, which on a small team is most of the panel.
   It used to be nothing — free space in a flex column, so the rod hung in unlit black and the
   bottom quarter of the surface read as canvas the design had not reached yet.
   It is a PLACE now, furnished the way the building furnishes a room nobody is standing in: the
   desk's own boards and lamp are already under it, the die-line prints where the next slip lands,
   and the stamp and its pad sit there whether or not there is anything to stamp. An empty desk,
   not an empty picture. */

.sp-clear {
  position: relative;
  z-index: 1;
  /* Sized entirely by what is left over, so it is exactly the stretch that used to be dead.
     The basis is 0 and NOT auto, and that is load-bearing rather than tidy: container-type size
     only takes effect on the block axis when the element's height cannot depend on its contents,
     and a 1 1 auto basis means a content-derived base size, so Chromium silently declines block
     containment and every height query below it never matches — while WIDTH queries on the
     same element keep working, which is what makes it look like a typo rather than a fallback. */
  flex: 1 1 0;
  container-type: size;
  container-name: clear;
  overflow: hidden;
}

/* The pad the work sits on. This is the piece that actually answers the complaint: boards and a
   lamp stop the stretch being black, but they do not stop it being UNBOUNDED, and an empty room
   reads as a room because it has EDGES — the building's empty rooms have walls and a floor line,
   not just a dark wall. So the clear desk gets an object with a rim, four corner mounts and a
   die-cut printed on it. The rod's shadow falls across it and the lamp falls down it, which is
   what keeps a large pad from being one flat value. */
.sp-blotter {
  position: absolute;
  left: 0; right: 3px; top: 3px; bottom: 5px;
  /* A pad is an OBJECT, so it has a size of its own. Stretched to fill whatever is left it stopped
     being a thing on the desk and became the desk, and on a quiet day that is a single beige
     rectangle for two thirds of the panel. Capped at roughly one work order deep — the pad is
     sized to what it is waiting for — and what shows below it is the desk's own boards, which is a
     surface with a grain and a lamp falling down it rather than a bigger version of the pad.
     max-height and not height: on a busy day there is less room than this and the pad then ends
     where the space does, keeping its rim, its corners and its bottom edge. */
  max-height: 264px;
  /* Only the softer of the two fibres: the pad is felt, not stock, and the paper tooth is
     calibrated against paper's own value — carried onto a darker surface unchanged it reads
     louder there than it does on the thing it was measured for. */
  background: var(--sp-pad-nap), var(--sp-pad);
  border: 1px solid var(--sp-desk-rim);
  /* The pile above throws onto it. One inset line is the whole difference between a pad lying on a
     desk and a rectangle painted on one. */
  box-shadow: inset 0 4px 7px -4px var(--sp-shadow);
}
.sp-corner { position: absolute; line-height: 0; }
.sp-corner svg { display: block; }
.sp-corner[data-at="tl"] { left: 1px; top: 1px; }
.sp-corner[data-at="tr"] { right: 1px; top: 1px; transform: rotate(90deg); }
.sp-corner[data-at="br"] { right: 1px; bottom: 1px; transform: rotate(180deg); }
.sp-corner[data-at="bl"] { left: 1px; bottom: 1px; transform: rotate(270deg); }
/* Below about two slips of clearance the mounts would meet in the middle and the pad would read as
   a diamond. The pad keeps its rim; the corners are what go. */
.sp-corner { display: none; }
@container clear (min-height: 62px) {
  .sp-corner { display: block; }
}

/* Where the next slip goes: the die-cut printed on the pad, hole and all. Dashed, unfilled, the
   pad showing through it — a shadow board, not another card. */
.sp-dieline {
  position: absolute;
  left: 9px; right: 12px; top: 11px;
  height: 27px;
  border: 1px dashed var(--sp-pad-rule);
}
.sp-dieline-hole {
  position: absolute;
  left: 5px; top: 6px;
  width: 12px; height: 12px;
  border-radius: 50%;
  border: 1px dashed var(--sp-pad-rule);
}
.sp-dieline-label {
  position: absolute;
  left: 22px; top: 8px;
  font-size: 9px;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--sp-dim);
}

/* What is standing on the wood. Held out of a short desk rather than cropped in half: below about
   a slip and a half of clearance there is no room for a prop, and half a stamp is worse than none.
   The query is on the desk's OWN height, which is the honest thing to ask. */
.sp-props {
  display: none;
  position: absolute;
  right: 22px; bottom: 12px;
  line-height: 0;
}
.sp-props svg { display: block; }
@container clear (min-height: 92px) {
  .sp-props { display: block; }
}

/* ── the slip you are writing ──────────────────────────────────────────────────────────────
   The composer is the last piece of paper on the same spike. This is the answer to two panels that
   do not belong to each other: not a shared colour scheme, a shared PIECE OF HARDWARE. */

.sp-compose {
  position: relative;
  z-index: 1;
  /* Sits on the floor of the panel — pushed there by the clear desk above it, which now owns that
     free space instead of an auto margin swallowing it. What opens up between the pile and this is
     DESK with a rod standing in it, which is the argument this whole direction rests on: the
     paperwork and the thing you type into are on one spike, on one surface. */
  padding: 6px 8px 8px 22px;
  background: var(--sp-grain), var(--sp-paper);
  border: 1px solid var(--sp-edge);
  box-shadow: 2px 2px 0 0 var(--sp-shadow);
}
.sp-compose-tag {
  font-size: 9px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--sp-dim);
}
.sp-compose-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 3px;
  padding-top: 4px;
  border-top: 1px dashed var(--sp-rule);
}
.sp-caret { color: var(--wp-ok); font-weight: 700; }
.sp-compose-ph { color: var(--sp-dim); font-style: italic; }

/* ── the people ────────────────────────────────────────────────────────────────────────────
   Imported whole from the workplace: the same grids, the same deterministic face per run id, the
   same pod colour on the shirt. Not a lookalike — the same rectangles. */

.sp-sprite { display: block; }
.sp-slip {
  --c-shirt: var(--accent, var(--wp-h1));
  --c-badge: color-mix(in srgb, var(--accent, var(--wp-h1)) 42%, var(--wp-ink));
  --c-eye: var(--wp-ink);
  --c-mouth: color-mix(in srgb, var(--wp-ink) 62%, #a8564a);
  --c-ghost: color-mix(in srgb, var(--wp-dim) 55%, var(--wp-bg));
  --c-ghost-ink: color-mix(in srgb, var(--wp-dim) 92%, var(--wp-bg));
}
.vscode-light .sp-slip,
.vscode-high-contrast-light .sp-slip {
  --c-shirt: color-mix(in srgb, var(--accent, var(--wp-h1)) 60%, var(--wp-tint));
}

/* Two frames, one asset, flipped on the shared beat — the same mechanism and the same tempo the
   building uses, so a person at the desk and a person on the board are working at one speed. */
/* The frame groups are named by the shared engine (drawFrames emits wp-f0/wp-f1), so these
   selectors are the workplace's, not this file's — which is the point. */
.wp-f0 { opacity: 1; }
.wp-f1 { opacity: 0; }
.sp-slip[data-status="running"][data-stalled="0"] .wp-f0 { animation: sp-fa var(--gait) steps(1, end) infinite; }
.sp-slip[data-status="running"][data-stalled="0"] .wp-f1 { animation: sp-fb var(--gait) steps(1, end) infinite; }
/* A stalled worker STOPS MOVING. Absence of motion is the reading — louder and cheaper than a
   timestamp, and the building already says it this way. */

@keyframes sp-fa {
  0%   { opacity: 1; animation-timing-function: steps(1, end); }
  50%  { opacity: 0; animation-timing-function: steps(1, end); }
  100% { opacity: 1; }
}
@keyframes sp-fb {
  0%   { opacity: 0; animation-timing-function: steps(1, end); }
  50%  { opacity: 1; animation-timing-function: steps(1, end); }
  100% { opacity: 0; }
}
@keyframes sp-pulse {
  0%   { opacity: .45; transform: scale(.86); animation-timing-function: cubic-bezier(.4, 0, .2, 1); }
  50%  { opacity: 1;   transform: scale(1);   animation-timing-function: cubic-bezier(.4, 0, .2, 1); }
  100% { opacity: .45; transform: scale(.86); }
}
@keyframes sp-flag {
  0%   { transform: skewY(0deg);   animation-timing-function: cubic-bezier(.45, 0, .55, 1); }
  50%  { transform: skewY(-7deg);  animation-timing-function: cubic-bezier(.45, 0, .55, 1); }
  100% { transform: skewY(0deg); }
}

@media (prefers-reduced-motion: reduce) {
  .sp-lamp svg, .sp-out svg, .sp-f0, .sp-f1 { animation: none !important; }
  .sp-f1 { opacity: 0; }
}

/* ── narrow ────────────────────────────────────────────────────────────────────────────────
   A side bar the user has squeezed. The gutter and the mug stay (they are the object), the
   typography tightens, and the parts of the foot that repeat information already on screen go. */

@container (max-width: 252px) {
  .sp { padding: 8px 6px 15px 3px; }
  .sp-order { padding-left: 20px; padding-right: 5px; }
  .sp-plate { margin-left: 20px; gap: 5px; }
  .sp-hole { left: 5px; }
  .sp-name { font-size: 11px; }
  .sp-said { font-size: 10px; -webkit-line-clamp: 3; }
  .sp-role, .sp-foot { font-size: 9px; }
  .sp-foot { gap: 6px; }
  .sp-slip[data-depth="1"] { margin-left: 6px; }
  .sp-staple { left: -11px; }
  /* The counterfoil and the perforations bleed to the card's edges, so both have to be re-cut when
     the card's own padding changes — otherwise the stub stops short of the paper and the punches
     run off it. */
  .sp-total { gap: 6px; margin-left: -20px; margin-right: -5px; padding-left: 20px; padding-right: 5px; }
  .sp-slip::before { left: -14px; right: -5px; }
  .sp-dieline { left: 7px; right: 9px; }
  .sp-dieline-hole { left: 4px; }
  .sp-dieline-label { left: 20px; }
  .sp-props { right: 16px; }
  .sp-slip[data-fold="slim"] .wp-stamp b { display: none; }
  .sp-slip[data-fold="slim"] .wp-stamp { padding: 2px 3px; }
  .sp-slip[data-fold="slim"]:hover .wp-stamp b,
  .sp-slip[data-fold="slim"]:focus-visible .wp-stamp b { display: inline; }
}
`;
