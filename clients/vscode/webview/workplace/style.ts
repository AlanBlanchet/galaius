/** The look of the place.
 *
 *  Three rules run through all of it.
 *
 *  COLOUR follows the editor. Walls, floors, light and paper are mixed from `--vscode-*` tokens,
 *  so the building is dim and warm in a dark theme and bright and cool in a light one without a
 *  second palette existing anywhere. Only the people keep literal tones — a person tinted by the
 *  editor background stops looking like a person.
 *
 *  MOTION follows one beat. Every ambient animation's duration is an integer ratio of `--beat`,
 *  so a floor full of screens, plants and steam moves as one thing rather than as thirty
 *  independent loops. Two deliberate exceptions: walking, driven by the ENGINE off the distance
 *  covered (a footfall on a CSS clock slides the moment a body speeds up), and the fan, which is
 *  the one thing in the building that genuinely turns at a constant rate.
 *
 *  The FRAME is a window, not a fit. The building is bigger than the panel on purpose and the
 *  camera goes to the work. Scaling the whole plan down to fit is what produced eighteen-pixel
 *  characters and six-pixel capability marks in the side bar this actually ships in.
 */

import { STAMP_CSS } from "./status";

export const STYLE = String.raw`
*, *::before, *::after { box-sizing: border-box; }
html, body, figure, figcaption, header, footer, p { margin: 0; padding: 0; }

body {
  font-family: var(--vscode-font-family, system-ui, sans-serif);
  color: var(--vscode-foreground, #ccc);
  background: var(--vscode-editor-background, #1e1e1e);
  font-size: 12px;
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}

.wp {
  --beat: 2.4s;

  --wp-bg: var(--vscode-editor-background, #1e1e1e);
  --wp-fg: var(--vscode-editor-foreground, #d4d4d4);
  /* The editor's description colour is tuned for the EDITOR background, not for a small label on
     a busy floor: it measures 3.95:1 at 10px in a light theme, under the 4.5 floor. */
  --wp-dim: color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 68%, var(--wp-fg));
  --wp-ink: #14101c;
  --wp-tint: #e8e4f0;

  --wp-h1: var(--vscode-charts-blue, #4f9cf5);
  --wp-h2: var(--vscode-charts-purple, #b180d7);
  --wp-h3: var(--vscode-charts-yellow, #d7ba7d);
  --wp-h4: var(--vscode-charts-green, #89d185);
  --wp-h5: var(--vscode-charts-orange, #d18616);
  --wp-h6: color-mix(in srgb, var(--wp-h1) 55%, var(--wp-h4));
  --wp-h7: color-mix(in srgb, var(--wp-h2) 55%, var(--wp-h5));
  --wp-ok: var(--wp-h4);
  --wp-bad: var(--vscode-errorForeground, #f14c4c);
  --wp-line: color-mix(in srgb, var(--wp-fg) 26%, var(--wp-bg));
  /* A room nobody is in. A VEIL rather than an absent glow: in a light theme "no light" left the
     empty room the BRIGHTEST thing on screen, so occupancy read backwards. */
  --wp-shut: color-mix(in srgb, var(--wp-ink) 18%, transparent);
  --wp-plate: color-mix(in srgb, var(--wp-fg) 82%, var(--wp-bg));
  --wp-paper: color-mix(in srgb, #f3efe4 82%, var(--wp-bg));
  --wp-chrome: color-mix(in srgb, var(--wp-bg) 92%, var(--wp-fg));

  /* ── the pixel-art light ────────────────────────────────────────────────────────────────
     ONE warm light and ONE cool shade for every ramp in the scene. Every material — a shirt, a
     desk top, a visitor's coat — derives its lit step by mixing toward the glint and its shaded
     step by mixing toward the shade, so twenty palettes still read as one scene under one sun
     out of the north-west. Shadows hue-shift COOL and highlights WARM because plain darkening
     reads muddy and plain lightening reads chalky — the oldest palette rule small pixel art
     has. */
  --px-glint: #ffdfae;
  --px-shade: #2c2344;

  /* ── the tile palette ───────────────────────────────────────────────────────────────────
     Every colour any tile may use, in one place. The art is authored against these names, so a
     light building and a dark one are the same drawings — no second tileset exists. */
  --t-floor: color-mix(in srgb, var(--wp-fg) 13%, var(--wp-bg));
  --t-floor-hi: color-mix(in srgb, var(--wp-fg) 20%, var(--wp-bg));
  --t-grout: color-mix(in srgb, var(--wp-fg) 19%, var(--wp-bg));
  --t-dais: color-mix(in srgb, var(--wp-h3) 22%, var(--wp-bg));
  --t-dais-hi: color-mix(in srgb, var(--wp-h3) 34%, var(--wp-bg));
  --t-carpet: color-mix(in srgb, var(--wp-fg) 11%, var(--wp-bg));
  --t-carpet-hi: color-mix(in srgb, var(--wp-fg) 16%, var(--wp-bg));
  --t-wall: color-mix(in srgb, var(--wp-fg) 24%, var(--wp-bg));
  --t-wall-cap: color-mix(in srgb, var(--wp-fg) 38%, var(--wp-bg));
  --t-wall-foot: color-mix(in srgb, var(--wp-ink) 60%, var(--wp-bg));
  /* The wall you LOOK AT rather than down on: lighter than the cap, with a skirting under it.
     One tile band on all four sides is a border; this is what makes it a room. */
  --t-face: color-mix(in srgb, var(--wp-fg) 31%, var(--wp-bg));
  --t-skirt: color-mix(in srgb, var(--wp-fg) 17%, var(--wp-bg));
  --t-shade: color-mix(in srgb, var(--wp-ink) 72%, var(--wp-bg));
  /* The passage has to look nothing like the places it joins, so the hall is the LIGHTEST ground
     in the building and the runner down the middle of it is the only warm one. */
  --t-hall: color-mix(in srgb, var(--wp-fg) 27%, var(--wp-bg));
  --t-hall-band: color-mix(in srgb, var(--wp-fg) 36%, var(--wp-bg));
  /* And the building's own mass has to look nothing like a floor: darker than any of them, so a
     room reads as carved out of a solid block rather than drawn on a sheet. */
  --t-mass: color-mix(in srgb, var(--wp-ink) 50%, var(--wp-bg));
  --t-rug: color-mix(in srgb, var(--wp-fg) 15%, var(--wp-bg));
  --t-rug-hi: color-mix(in srgb, var(--wp-fg) 23%, var(--wp-bg));
  --t-runner: color-mix(in srgb, var(--wp-h5) 18%, var(--wp-bg));
  --t-runner-hi: color-mix(in srgb, var(--wp-h5) 28%, var(--wp-bg));
  --t-screen: color-mix(in srgb, var(--wp-h1) 74%, var(--wp-bg));
  --t-warm: color-mix(in srgb, var(--wp-h3) 62%, var(--wp-bg));
  --t-mat: color-mix(in srgb, var(--wp-h3) 26%, var(--wp-bg));
  --t-wood: color-mix(in srgb, #73583f 52%, var(--wp-bg));
  --t-wood-hi: color-mix(in srgb, #9b7652 58%, var(--wp-bg));
  --t-metal: color-mix(in srgb, var(--wp-fg) 46%, var(--wp-bg));
  --t-lit: var(--wp-h4);
  --t-glass: color-mix(in srgb, #9fd6e0 52%, var(--wp-bg));
  --t-leaf: color-mix(in srgb, var(--wp-h4) 74%, var(--wp-bg));
  --t-ground: color-mix(in srgb, var(--wp-h4) 16%, var(--wp-bg));
  --t-ground-hi: color-mix(in srgb, var(--wp-h4) 22%, var(--wp-bg));
  --t-path: color-mix(in srgb, var(--wp-h3) 30%, var(--wp-bg));
  --t-path-hi: color-mix(in srgb, var(--wp-h3) 40%, var(--wp-bg));
  --t-fabric: color-mix(in srgb, var(--wp-h5) 40%, var(--wp-bg));
  --t-book: color-mix(in srgb, var(--wp-h2) 62%, var(--wp-bg));
  --t-book2: color-mix(in srgb, var(--wp-h3) 62%, var(--wp-bg));
  --t-ink: color-mix(in srgb, var(--wp-ink) 78%, var(--wp-bg));

  /* THE GROUNDS. Three greens, not one: a canopy painted in a single tone is a blob whatever
     shape it is cut to, and the whole reason the old outdoor props read as floating is that a
     flat green triangle has no top and no underside. Lit on the north-west shoulder, mid through
     the body, shade under the south-east — the same sun the building's walls are lit by. */
  /* THREE GREENS THAT ARE ACTUALLY THREE. At 82 / 62 / 38 percent of one hue the canopy came out
     a single mint blob at any distance — the tones have to straddle the LAWN's own value, not sit
     beside it, or a tree is a lighter rectangle of grass. Lit shoulder well above the ground,
     shade well below it, and the shade mixed toward ink because that is the only direction with
     anything under it in either theme. */
  --t-leaf-hi: color-mix(in srgb, var(--wp-h4) 96%, var(--wp-bg));
  --t-leaf-lo: color-mix(in srgb, var(--wp-h4) 52%, var(--wp-ink));
  /* The forest floor, under the canopies. Darker than the lawn in BOTH themes — it stands in for
     the permanent shade of a wood at the rungs where a cast shadow is three device pixels.
     Darkened through the LEAF-SHADE green, never through bare ink: ink alone turns the clearing
     grey and a grey patch under a green tree reads as pavement, not understory. */
  --t-under: color-mix(in srgb, var(--t-ground) 52%, var(--t-leaf-lo));
  --t-bark: color-mix(in srgb, #6b4423 78%, var(--wp-bg));
  --t-bark-hi: color-mix(in srgb, #97663a 78%, var(--wp-bg));
  --t-stone: color-mix(in srgb, var(--wp-fg) 30%, var(--wp-bg));
  --t-stone-hi: color-mix(in srgb, var(--wp-fg) 44%, var(--wp-bg));
  --t-bloom: color-mix(in srgb, var(--wp-h3) 84%, var(--wp-bg));
  --t-water: color-mix(in srgb, var(--wp-h1) 40%, var(--wp-bg));
  --t-water-hi: color-mix(in srgb, var(--wp-h1) 58%, var(--wp-bg));

  /* ── the light model ────────────────────────────────────────────────────────────────────
     Depth in this view comes from LIGHT, not from the theme. A room is dark and its lamps carve
     quantised pools out of that dark; every wall drops a hard band; every standing thing drops
     its own silhouette. Which is why the two themes are two TIMES OF DAY rather than one picture
     with its contrast drained: at night the pools are most of what you can see, and by day the
     range comes from shadow instead. */
  --l-lamp: var(--wp-h3);
  --l-tint: var(--l-lamp);
  --l-mix: 0%;
  /* Night. The Kenney tiles carry their own value structure, so the light layer is a WASH over
     finished art now, not the thing that makes the picture: pools a third of their old strength,
     and the unlit veil a grey rather than a blackout — an empty room reads closed, never like a
     hole in the building. */
  --l-0: 14%;
  --l-1: 8%;
  --l-2: 3%;
  --l-edge: color-mix(in srgb, var(--wp-ink) 10%, transparent);
  --l-unlit: color-mix(in srgb, var(--wp-ink) 22%, transparent);
  --l-ao: color-mix(in srgb, var(--wp-ink) 32%, transparent);
  --wp-drop: #05040d;
  /* The one cast shade left on a body: a soft contact patch, because a DOM sprite floats over
     the map and needs a toe-hold the baked art cannot give it. */
  --wp-foot: color-mix(in srgb, var(--wp-drop) 26%, transparent);

  --stamp-ink: var(--wp-fg);
  --stamp-quiet: var(--wp-dim);
  --stamp-mix: var(--wp-bg);
  --stamp-bg: var(--wp-bg);
  --stamp-shadow: var(--wp-ink);

  display: flex;
  flex-direction: column;
  height: 100vh;
  padding: 3px;
  gap: 0;
  /* Inherited by every svg in the tree, so the Kenney art upscales hard-edged everywhere —
     the map, the dolls, the defs — not only inside the stagebox. */
  image-rendering: pixelated;
}

/* A light theme has to flip the tint dark, or every team colour pastels out against cream. */
body.vscode-light .wp,
body.vscode-high-contrast-light .wp {
  --wp-tint: #2f2a3a;
  --t-wall-foot: color-mix(in srgb, var(--wp-ink) 32%, var(--wp-bg));
  --t-carpet: color-mix(in srgb, color-mix(in srgb, var(--wp-h1) 26%, var(--wp-fg)) 30%, var(--wp-bg));
  --t-carpet-hi: color-mix(in srgb, color-mix(in srgb, var(--wp-h1) 26%, var(--wp-fg)) 40%, var(--wp-bg));
  --t-shade: color-mix(in srgb, var(--wp-ink) 46%, var(--wp-bg));

  /* THE GROUND HAS TO BE A MID-TONE, or light has nothing to lift and shadow nothing to drop.
     Every ground tile is mixed from the editor foreground toward the background, and on a WHITE
     background thirteen percent of a dark grey is L* 92 — so the floor was already brighter than
     any lamp could make it and the pools came out as pastel stickers on paper. */
  --t-floor: color-mix(in srgb, var(--wp-fg) 28%, var(--wp-bg));
  --t-floor-hi: color-mix(in srgb, var(--wp-fg) 20%, var(--wp-bg));
  --t-grout: color-mix(in srgb, var(--wp-fg) 34%, var(--wp-bg));
  --t-rug: color-mix(in srgb, var(--wp-fg) 30%, var(--wp-bg));
  --t-rug-hi: color-mix(in srgb, var(--wp-fg) 40%, var(--wp-bg));
  --t-hall: color-mix(in srgb, var(--wp-fg) 20%, var(--wp-bg));
  --t-hall-band: color-mix(in srgb, var(--wp-fg) 30%, var(--wp-bg));

  /* AND THE WALL INVERTS. More foreground means LIGHTER on a dark background and DARKER on a
     light one, so a cap tuned to be the brightest surface at night was the darkest by day and the
     wall read as advancing out of the floor. The cap catches the light in both themes; the FACE —
     the vertical surface you look at — is in shade in both. */
  --t-wall: color-mix(in srgb, var(--wp-fg) 34%, var(--wp-bg));
  --t-wall-cap: color-mix(in srgb, var(--wp-fg) 19%, var(--wp-bg));
  --t-face: color-mix(in srgb, var(--wp-fg) 46%, var(--wp-bg));
  --t-skirt: color-mix(in srgb, var(--wp-fg) 60%, var(--wp-bg));
  /* The building's mass has to be the DARKEST thing in either theme. At 34% of a near-black over
     white it came out mid-grey — brighter than several room floors — so the structure advanced
     instead of receding and the whole plan flattened. It is the single biggest reason the light
     theme read as washed out. */
  --t-mass: color-mix(in srgb, var(--wp-ink) 62%, var(--wp-bg));

  /* By day the grounds are the brightest thing on screen and every green mixed toward a WHITE
     background goes to pastel — the same bottomless-mix problem the floors had. So the leaf
     tones are mixed toward the INK instead, which is the only surface with anything under it. */
  --t-ground: color-mix(in srgb, var(--wp-h4) 25%, var(--wp-bg));
  --t-ground-hi: color-mix(in srgb, var(--wp-h4) 32%, var(--wp-bg));
  --t-leaf: color-mix(in srgb, var(--wp-h4) 74%, var(--wp-bg));
  --t-leaf-hi: color-mix(in srgb, var(--wp-h4) 92%, var(--wp-bg));
  --t-leaf-lo: color-mix(in srgb, var(--wp-h4) 62%, var(--wp-ink));
  /* Day: the same rule through the theme's own darker green — grey kills it on cream. */
  --t-under: color-mix(in srgb, var(--t-ground) 55%, var(--t-leaf-lo));
  --t-stone: color-mix(in srgb, var(--wp-fg) 34%, var(--wp-bg));
  --t-stone-hi: color-mix(in srgb, var(--wp-fg) 22%, var(--wp-bg));

  /* DAY. The sun is the fixture, so the pools are weaker and the SHADOWS carry the range: a hard
     shadow on a bright floor is the whole reason a daylit pixel scene reads as three-dimensional.
     Every one of these numbers is per-theme on purpose — the same alpha buys about four times the
     ink on a light substrate, so copying a night value across is how the shadow ends up either
     invisible or a black bar. */
  --l-lamp: var(--wp-h5);
  --l-0: 12%;
  --l-1: 7%;
  --l-2: 3%;
  --l-edge: color-mix(in srgb, #2b3348 8%, transparent);
  --l-unlit: color-mix(in srgb, #2b3348 18%, transparent);
  --l-ao: color-mix(in srgb, #232a3d 22%, transparent);
  --wp-drop: #060a14;
  --wp-foot: color-mix(in srgb, var(--wp-drop) 22%, transparent);
}

/* ── the window ────────────────────────────────────────────────────────────────────────────
   A bounded viewport with a building inside it that is deliberately bigger. The camera lives in
   the engine and arrives here as one transform on the stage, so every tile, sprite and route
   stays in exact proportion and nothing ever reflows. */

/* Scenery is not selectable prose. A drag starting on a speech bubble or a nameplate used to
   begin a native text selection instead of panning the map, which reads as the map jamming. */
.wp-say, .wp-tag, .wp-plaque, .wp-stamp, .wp-kit, .wp-fac { user-select: none; -webkit-user-select: none; }
.wp-view {
  position: relative;
  flex: 1 1 auto;
  min-height: 180px;
  overflow: hidden;
  border: 2px solid var(--wp-line);
  background: color-mix(in srgb, var(--wp-ink) 30%, var(--wp-bg));
  cursor: grab;
}
.wp-view.is-dragging { cursor: grabbing; }
.wp-stagebox {
  position: absolute;
  left: 0;
  top: 0;
  transform-origin: 0 0;
  image-rendering: pixelated;
  /* NO will-change. Promoted, the stage rasters once at whatever scale Chromium picks and the
     camera's scale() then stretches that RASTER — measured as the whole map going bilinear-soft
     at zoom 2 while the HTML dolls beside it stayed crisp. Un-promoted, a scale change
     re-rasters the visible tiles at the true scale and every source pixel stays square. */
}
/* The grounds run past the world, so the map paints outside its own box on purpose. */
.wp-map { position: absolute; inset: 0; display: block; overflow: visible; }
.wp-world { display: none; }

/* Daylight crossing the building, and the dust hanging in it. The two slowest things on screen:
   a place with a time of day reads as a place even when nobody in it is moving. */
.wp-view::after {
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 400;
  background: linear-gradient(105deg,
    transparent 0%,
    color-mix(in srgb, var(--wp-h3) 8%, transparent) 42%,
    transparent 74%);
  background-size: 260% 100%;
  animation: wp-daylight calc(var(--beat) * 25) ease-in-out infinite alternate;
}
@keyframes wp-daylight { from { background-position: 0% 0; } to { background-position: 100% 0; } }
.wp-view::before {
  content: "";
  position: absolute;
  inset: -40px;
  pointer-events: none;
  z-index: 390;
  opacity: .5;
  background-image:
    radial-gradient(circle, color-mix(in srgb, var(--wp-h3) 55%, transparent) 1px, transparent 1.6px),
    radial-gradient(circle, color-mix(in srgb, var(--wp-h3) 34%, transparent) 1px, transparent 1.6px);
  background-size: 140px 110px, 90px 170px;
  animation: wp-motes calc(var(--beat) * 18) linear infinite;
}
@keyframes wp-motes {
  from { background-position: 0 0, 40px 20px; }
  to { background-position: 90px -110px, -60px -170px; }
}

/* ── the board by the door ─────────────────────────────────────────────────────────────────
   One strip laid OVER the world, not a band beside it. The complaint that this reads as an ops
   dashboard was mostly volume of chrome: a stats bar above, a ten-item legend below, and a row
   of marks under every head. The legend is gone (the marks carry their own words when you point
   at them), and what is left is a line thin enough that the floor is the biggest thing on screen
   at any panel width. */

.wp-hud {
  position: absolute;
  z-index: 500;
  left: 0;
  right: 0;
  top: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 6px;
  font-size: 10px;
  background: var(--wp-chrome);
  border-bottom: 1px solid var(--wp-line);
  /* The strip is CHROME, and chrome eats its own clicks: with pointer-events none a click on
     "2 notes" fell through to the map behind it, and a double-click there silently ran the
     whole-floor zoom — the "silent zoom jump" finding. */
  pointer-events: auto;
  overflow: hidden;
  white-space: nowrap;
}
.wp-sign { display: inline-flex; align-items: baseline; gap: 5px; }
.wp-sign-name { font-weight: 700; letter-spacing: .12em; text-transform: uppercase; font-size: 10px; }
.wp-sign-sub { color: var(--wp-dim); font-size: 9px; }
.wp-chip { display: inline-flex; align-items: center; gap: 3px; color: var(--wp-dim); }
.wp-chip b { color: var(--wp-fg); }
.wp-spend { color: var(--wp-dim); margin-left: auto; }

/* ── the survey panel ─────────────────────────────────────────────────────────────────────
   The camera takes the overview away; this gives it back, and gives the reader the camera.

   What shipped before was the plan alone: 96x72, aria-hidden, click to jump, and nothing else
   — no scale control at all, because the scale was derived from the panel width. So the one
   thing anybody wants from a map they are lost in, PULLING BACK, was not on offer at any
   setting, and the panning that did exist announced itself with a cursor and undid itself six
   seconds later. This is that corner rebuilt as an instrument: the plan, a rule of scale marks,
   and two latches that say in words what the camera is doing. */

.wp-plan {
  position: absolute;
  z-index: 500;
  right: 5px;
  bottom: 5px;
  width: 118px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 8px;
  letter-spacing: .06em;
  text-transform: uppercase;
  font-weight: 700;
}
.wp-mini {
  position: relative;
  height: 74px;
  background: var(--wp-chrome);
  border: 1px solid var(--wp-line);
  box-shadow: 2px 2px 0 var(--wp-ink);
  cursor: crosshair;
}
.wp-mini:focus-visible { outline: 1px solid var(--wp-fg); outline-offset: 1px; }
.wp-mini svg { position: absolute; inset: 2px; width: calc(100% - 4px); height: calc(100% - 4px); }
.wp-mini .mm-bg { fill: color-mix(in srgb, var(--wp-fg) 10%, var(--wp-bg)); }
.wp-mini .mm-r { fill: color-mix(in srgb, var(--wp-fg) 26%, var(--wp-bg)); }
.wp-mini .mm-hall { fill: color-mix(in srgb, var(--wp-h5) 34%, var(--wp-bg)); }
.wp-mini .mm-r[data-lit="1"] { fill: color-mix(in srgb, var(--wp-h3) 52%, var(--wp-bg)); }
.wp-eye {
  position: absolute;
  border: 1px solid var(--wp-fg);
  background: color-mix(in srgb, var(--wp-fg) 12%, transparent);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--wp-ink) 60%, transparent);
  pointer-events: none;
}
.wp-dots { position: absolute; inset: 2px; pointer-events: none; }
.wp-dots i {
  position: absolute;
  width: 3px;
  height: 3px;
  margin: -1px 0 0 -1px;
  background: var(--accent, var(--wp-h1));
}

/* ── the scale rule ───────────────────────────────────────────────────────────────────────
   The ladder drawn as what it is. Nine marks of rising height, the one you are standing on lit
   and the ones below it half-lit, so the rule reads as a filled gauge rather than as nine
   identical dots — you can see at a glance how far back you are and how much further there is
   to go. A cap at each end because minus and plus are the two symbols nobody has to learn. */

.wp-rule {
  display: flex;
  align-items: stretch;
  gap: 1px;
  height: 16px;
  background: var(--wp-chrome);
  border: 1px solid var(--wp-line);
  box-shadow: 2px 2px 0 var(--wp-ink);
}
.wp-rungs {
  flex: 1 1 auto;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1px;
  padding: 0 2px 2px;
  cursor: ew-resize;
}
.wp-rungs i {
  flex: 1 1 auto;
  height: var(--h, 4px);
  /* Faint. The unlit marks are the TALL ones, so at any weight they out-mass the lit one and
     the rule reads as a grey block with a speck in it rather than as a gauge. */
  background: color-mix(in srgb, var(--wp-fg) 15%, var(--wp-bg));
}
/* The gauge. data-step is the rung the camera is on; every mark up to it is filled and the
   rung itself is lit — so the rule reads how far back you are AND how much further it goes,
   which nine identical dots could not. nth-child(-n+N) is what keeps this in the stylesheet
   instead of costing nine element writes on every frame the camera moves. */
.wp-rule[data-step="1"] .wp-rungs i:nth-child(-n+1),
.wp-rule[data-step="2"] .wp-rungs i:nth-child(-n+2),
.wp-rule[data-step="3"] .wp-rungs i:nth-child(-n+3),
.wp-rule[data-step="4"] .wp-rungs i:nth-child(-n+4),
.wp-rule[data-step="5"] .wp-rungs i:nth-child(-n+5),
.wp-rule[data-step="6"] .wp-rungs i:nth-child(-n+6) {
  background: color-mix(in srgb, var(--wp-fg) 60%, var(--wp-bg));
}
.wp-rule[data-step="1"] .wp-rungs i:nth-child(1),
.wp-rule[data-step="2"] .wp-rungs i:nth-child(2),
.wp-rule[data-step="3"] .wp-rungs i:nth-child(3),
.wp-rule[data-step="4"] .wp-rungs i:nth-child(4),
.wp-rule[data-step="5"] .wp-rungs i:nth-child(5),
.wp-rule[data-step="6"] .wp-rungs i:nth-child(6) {
  background: var(--wp-fg);
}
.wp-rungs i { transition: background 120ms linear; }
.wp-read {
  align-self: center;
  padding: 0 4px 0 2px;
  min-width: 20px;
  text-align: right;
  color: var(--wp-fg);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0;
}

/* ── the two latches ──────────────────────────────────────────────────────────────────────
   Full-width, labelled in words, and each one says what it does rather than what it is. */

.wp-cam {
  font: inherit;
  color: var(--wp-dim);
  background: var(--wp-chrome);
  border: 0;
  padding: 0;
  cursor: pointer;
}
.wp-cam-step {
  width: 15px;
  font-size: 12px;
  line-height: 1;
  letter-spacing: 0;
  color: var(--wp-fg);
}
.wp-cam-step:hover { background: color-mix(in srgb, var(--wp-fg) 18%, var(--wp-chrome)); }
.wp-cam-wide {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 16px;
  padding: 0 5px;
  border: 1px solid var(--wp-line);
  box-shadow: 2px 2px 0 var(--wp-ink);
  color: var(--wp-fg);
}
.wp-cam-wide:hover { background: color-mix(in srgb, var(--wp-fg) 16%, var(--wp-chrome)); }
.wp-cam-wide:active { transform: translate(1px, 1px); box-shadow: 1px 1px 0 var(--wp-ink); }
.wp-cam:focus-visible { outline: 1px solid var(--wp-fg); outline-offset: 1px; }
.wp-cam-mark { width: 9px; height: 9px; flex: 0 0 auto; fill: currentColor; display: block; }

/* THE SURVEY IS THE RESTING TRUTH, so the latch is quiet by default and lights only while the
   camera is genuinely chasing the work — an ACTIVE state the reader opted into, said in the
   accent, held steady. The old breathing amber "RESUME FOLLOW" was right when following was the
   default; as chrome on a still floor it was the loudest thing on screen. */
.wp-cam-follow .wp-on { display: none; }
.wp-view[data-follow="1"] .wp-cam-follow .wp-on { display: inline; }
.wp-view[data-follow="1"] .wp-cam-follow .wp-off { display: none; }
.wp-view[data-follow="1"] .wp-cam-follow {
  color: var(--wp-bg);
  background: var(--wp-h3);
  border-color: var(--wp-h3);
}

/* ── pulled back ──────────────────────────────────────────────────────────────────────────
   Below one, a nameplate held at constant SCREEN size is wider than the room the person stands
   in — the words out-mass the building, which is the complaint the constant-size trick exists
   to prevent, arriving from the other side. So the far view sheds its words. What is left is a
   signed plan: room signs (a plan is signed), the light, and a person as their own colour. */

.wp-view[data-far="1"] .wp-say,
.wp-view[data-far="1"] .wp-kit { display: none; }
/* The names STAY at the plan rung — the cold open lands here, and a floor with no names was
   the owner's literal complaint. Same solution the plaques earned: plan type at constant
   SCREEN size (counter-scaled by --inv, which the stage already carries), name only, capped
   narrow so neighbours mostly clear each other, and staggered checkerboard so a shared bench
   reads as two rows of labels rather than one collision. */
.wp-view[data-far="1"] .wp-tag {
  transform: translateX(-50%) scale(var(--inv, 1));
  transform-origin: top center;
  font-size: 8px; letter-spacing: 0; padding: 0 2px;
  max-width: 40px; opacity: .92;
}
.wp-view[data-far="1"] .wp-tag > i { display: none; }
/* Staggered by TILE phase (the engine stamps data-ph from (x+2y) mod 3), never by nth-child:
   DOM order says nothing about spatial adjacency. THREE rows, because 2-parity leaves
   distance-2 neighbours on one row.
   The far offsets COUNTER-SCALE with the glyph: the plate's size rides --inv back to constant
   screen size, so an offset left in stage space shrinks with the floor — measured at FIT
   (stage 0.342) the 12px offset became ~4px against a 13px glyph and 8 of 45 plates still
   collided. calc(px * --inv) keeps the row separation one glyph tall at every floor size. */
.wp-view[data-far="1"] .wp-actor[data-ph="1"] .wp-tag { top: calc(3px + 13px * var(--inv, 1)); bottom: auto; }
.wp-view[data-far="1"] .wp-actor[data-ph="2"] .wp-tag { top: calc(3px + 26px * var(--inv, 1)); bottom: auto; }
/* Near and mid zoom scale plate and offset together, so plain pixels hold there. */
.wp-actor[data-ph="1"] .wp-tag { top: 14px; bottom: auto; }
.wp-actor[data-ph="2"] .wp-tag { top: 25px; bottom: auto; }
/* The up-label flip is a SECOND anchoring scheme on the same element (bottom-anchored above
   the head). The stagger must COMPOSE with it, not fight it: setting top while its bottom
   stays set over-constrains the absolute box and CSS silently collapses it to ~1px — 20 of 45
   plates rendered BLANK before these compound rules (the re-verdict's find). Up-labelled
   actors stagger on BOTTOM, top released. */
.wp-actor[data-label="up"][data-ph="1"] .wp-tag { top: auto; bottom: calc(var(--head, 32px) + 18px); }
.wp-actor[data-label="up"][data-ph="2"] .wp-tag { top: auto; bottom: calc(var(--head, 32px) + 29px); }
.wp-view[data-far="1"] .wp-actor[data-label="up"][data-ph="1"] .wp-tag {
  top: auto; bottom: calc(var(--head, 32px) + 4px + 13px * var(--inv, 1)); }
.wp-view[data-far="1"] .wp-actor[data-label="up"][data-ph="2"] .wp-tag {
  top: auto; bottom: calc(var(--head, 32px) + 4px + 26px * var(--inv, 1)); }
.wp-view[data-far="1"] .wp-can { opacity: 0; pointer-events: none; }
.wp-view[data-far="1"] .wp-plaque {
  /* Pulled back, the plaques are the ONLY words left on the plan — the tags, bubbles and kits
     are all shed — so this is where the type budget goes, not where it is cut. 6px was chosen
     to FIT the narrowest room and measured unreadable at the whole-floor rung; a plan whose one
     job is "which room is which" signs its rooms in plan type instead. */
  font-size: 9px;
  padding: 1px 3px;
  letter-spacing: 0;
  /* A room pulled back is short of WIDTH, never of height, so a sign that wraps says more
     than one that ellipses: "PRODUCTION & MAKERS" over two lines beats "PRODUCTIO...". And a
     NARROW room (the web yard is a five-tile strip) caps its sign below one readable word, so
     the cap takes a floor: at plan scale a label overflowing its slip of ground is how any map
     labels a narrow feature. */
  max-width: max(72px, calc(var(--rw, 8) * 32px * var(--z, 1)));
  white-space: normal;
  line-height: 1.15;
  text-align: center;
}


/* ── the rooms ─────────────────────────────────────────────────────────────────────────────*/

/* A room with somebody in it is LIT — and the light is a POOL, banded on the tile grid, with the
   hue of what that room is DOING. "Where is everyone" and "who is in trouble" are both answered
   by the colour and shape of the light, before a single label resolves.

   data-voice is the engine's, and its values are the rail's own Attention names: the panel
   that prints the word ERROR in charts-red is the panel this room is lit by. */
.wp-pool {
  /* The mix is written HERE, not hoisted into a token on the container. A custom property
     substitutes its var()s at the element that DECLARES it, so a --l-hue declared on .wp resolved
     --l-tint from .wp and inherited that finished colour down — every room, including the one
     with a crashed agent in it, came out the same lamp yellow. Nesting the mix at the point of use
     is what lets an ancestor data-voice reach it. */
  fill: color-mix(in srgb,
    color-mix(in srgb, var(--l-tint) var(--l-mix), var(--l-lamp)) var(--l-a, 0%), transparent);
  opacity: 0;
  transition: opacity 520ms cubic-bezier(.33, 0, .2, 1), fill 700ms ease-in-out;
}
.wp-pool[data-l="0"] { --l-a: var(--l-0); }
.wp-pool[data-l="1"] { --l-a: var(--l-1); }
.wp-pool[data-l="2"] { --l-a: var(--l-2); }
/* The two outer bands are SHADE, not weaker light, and that is the whole difference between a
   ramp and a tint: three decreasing washes of one hue over one floor move the value by a handful
   of points and read as a coloured rectangle — which is exactly the flat category-fill this layer
   replaced. Light near the fixture, dark away from it, is what a light map actually does.
   They are also never faded: the far corner of a room is dark whether or not anybody is home. */
.wp-pool[data-l="3"] { fill: var(--l-edge); opacity: 1; }
.wp-pool[data-l="4"] { fill: var(--l-unlit); opacity: 1; }
.wp-rm[data-lit="1"] .wp-pool { opacity: 1; }
.wp-shut {
  fill: var(--wp-shut);
  opacity: 1;
  transition: opacity 520ms cubic-bezier(.33, 0, .2, 1);
}
.wp-rm[data-lit="1"] .wp-shut { opacity: 0; }

/* What each state does to the light. Only the states that WANT something take the room's colour
   over; work in progress keeps the lamp warm, or a floor of eleven busy agents would be eleven
   blue rooms and the one that needs a person would not stand out at all.

   A voiced room also runs a HOTTER alpha ladder than the resting lamp. At the lamp's own 14/8/3
   the voice was only a hue swap inside a wash that was already faint — forcing a room to error
   moved the whole-floor mean by two parts in 255, i.e. the state existed and could not be seen.
   The purpose of this layer is to answer "who is in trouble" ACROSS THE MAP, so a state that
   wants a person takes the room, not a tenth of it. ERROR burns loudest, the rest step down in
   urgency; the ramp SHAPE survives (bands still decrease, the far corners stay shade), which is
   what keeps a hot room reading as a room on fire rather than a painted rectangle. */
.wp-lit[data-voice="error"] {
  --l-tint: var(--vscode-charts-red, #f14c4c); --l-mix: 100%;
  --l-0: 58%; --l-1: 42%; --l-2: 26%;
}
.wp-lit[data-voice="asked"] {
  --l-tint: var(--vscode-charts-blue, #4daafc); --l-mix: 88%;
  --l-0: 46%; --l-1: 32%; --l-2: 18%;
}
.wp-lit[data-voice="held"] {
  --l-tint: var(--vscode-charts-yellow, #d7ba7d); --l-mix: 74%;
  --l-0: 38%; --l-1: 26%; --l-2: 14%;
}
/* FINISHED earns more of the room than it used to, because it now means something different: it
   fires only when EVERY run in a department is done, not when any one of them is. At 46% — a mix
   tuned back when one done agent could trigger it — it was a barely-warmer grey, invisible at the
   scale somebody actually asks "is anything still running?" at. It is a rare, unanimous, whole-
   room fact now, and it is the only thing at whole-floor scale that carries it. */
.wp-lit[data-voice="finished"] {
  --l-tint: var(--vscode-charts-green, #89d185); --l-mix: 82%;
  --l-0: 40%; --l-1: 28%; --l-2: 15%;
}

/* THE RIM — the room's floor outlined in the voice's own colour, held at SCREEN width like the
   labels are. The pool is area and area shrinks with the map; an edge does not, so at the
   whole-floor rung — where a tile is eight device pixels and any wash is texture — the rim is
   the mark that still says WHICH room wants a person. Constant screen width for the same reason
   the plaque is: it is read, not looked at. */
.wp-voice-rim {
  fill: none;
  stroke: var(--l-tint);
  stroke-width: 2.5;
  vector-effect: non-scaling-stroke;
  opacity: 0;
  pointer-events: none;
  transition: opacity 520ms cubic-bezier(.33, 0, .2, 1);
}
.wp-lit[data-voice] .wp-voice-rim { opacity: .95; }
.wp-lit[data-voice="error"] .wp-voice-rim {
  animation: wp-rim-alarm calc(var(--beat) * 2 / 3) ease-in-out infinite;
}
@keyframes wp-rim-alarm {
  0%, 100% { opacity: .45; }
  50% { opacity: 1; }
}

/* And the fixture itself differs by trade: a machine room burns colder than a library. The old
   flat wash carried this and it was worth keeping — it is most of what tells two lit rooms apart
   when everyone in both of them is simply working. */
/* THE LIGHT BREATHES WITH THE WORK, and that is the point of tying it to the taxonomy at all: a
   static colour says what state a room is in, a rhythm says the room is LIVE. Both are on the one
   beat everything else in this building runs on, and both are eased — a light that steps linearly
   between two levels reads as a switch being flicked, not as a lamp.

   ERROR takes the stamp's own alarm tempo, so the placard over the head and the light in the room
   pulse together rather than beating against each other. Everything else takes a slow four-beat
   breath, shallow enough that a floor of eleven working agents is alive rather than strobing. */
.wp-lit[data-lit="1"] .wp-pool[data-l="0"],
.wp-lit[data-lit="1"] .wp-pool[data-l="1"] {
  animation: wp-breathe calc(var(--beat) * 4) cubic-bezier(.45, .05, .55, .95) var(--d, 0s) infinite;
}
/* fill-opacity, NOT opacity: the element's own opacity is what fades a room in when somebody
   walks into it, and an animation on the same property wins outright over the declaration, so the
   room stopped fading and started snapping on. The two multiply. */
@keyframes wp-breathe {
  0%, 100% { fill-opacity: .84; }
  50% { fill-opacity: 1; }
}
.wp-lit[data-voice="error"] .wp-pool[data-l="0"],
.wp-lit[data-voice="error"] .wp-pool[data-l="1"],
.wp-lit[data-voice="error"] .wp-pool[data-l="2"] {
  animation: wp-alarm calc(var(--beat) * 2 / 3) ease-in-out infinite;
}
@keyframes wp-alarm {
  0%, 100% { fill-opacity: .55; }
  50% { fill-opacity: 1; }
}

/* The passage burns a cooler, flatter light than any room off it: overheads in a corridor, not
   lamps on a desk. It is also the one light in the building that is never switched off. */
.wp-lit[data-kind="hall"] { --l-lamp: color-mix(in srgb, var(--wp-h1) 34%, var(--wp-fg)); }
.wp-lit[data-kind="machine"] { --l-lamp: color-mix(in srgb, var(--wp-h1) 62%, var(--wp-h3)); }
.wp-lit[data-kind="signals"] { --l-lamp: color-mix(in srgb, var(--wp-h6) 58%, var(--wp-h3)); }
.wp-lit[data-kind="stacks"] { --l-lamp: color-mix(in srgb, var(--wp-h2) 40%, var(--wp-h3)); }
.wp-lit[data-kind="boardroom"] { --l-lamp: color-mix(in srgb, var(--wp-h5) 48%, var(--wp-h3)); }
.wp-lit[data-kind="drafting"] { --l-lamp: color-mix(in srgb, var(--wp-h4) 32%, var(--wp-h3)); }

/* Every wall in the building, casting into the floor beside it. ONE path for the whole world, so
   the overlaps at a corner paint once rather than stacking into a black notch. */
.wp-ao { fill: var(--l-ao); pointer-events: none; }


/* The rug is bordered by a stroke rather than by its own tile: a pattern repeats the border in
   every cell and the floor comes out a chequerboard, which is louder than the carpet it replaced. */
.wp-rug { fill: none; stroke: var(--t-rug-hi); stroke-width: 1; }
/* The roof's rim, in the pack's own parapet pink. */
.wp-parapet { fill: none; stroke: #e0a89b; stroke-width: 3; opacity: .85; }
.wp-core { animation: wp-core calc(var(--beat) * 1.5) ease-in-out infinite; transform-origin: center; }
@keyframes wp-core { 0%, 100% { opacity: .72; } 50% { opacity: 1; } }

/* ── the building is ALIVE when nobody is working ──────────────────────────────────────────
   Every one of these is an overlay on a prop that is already there, never a redraw of it, and
   every duration is a ratio of the one beat. The point is not decoration: a floor where a screen
   flickers, a kettle steams, a fan turns and the plants move is a place somebody LIVES in, and it
   is the difference between a still picture of a workplace and a workplace. */

.wp-lv { pointer-events: none; }
.wp-lv-screen {
  opacity: .55;
  animation: wp-flick calc(var(--beat) * 2) steps(1, end) infinite;
  animation-delay: var(--d, 0s);
}
@keyframes wp-flick {
  0%, 22% { opacity: .30; }
  23%, 47% { opacity: .62; }
  48%, 51% { opacity: .18; }
  52%, 78% { opacity: .55; }
  79%, 100% { opacity: .38; }
}
.wp-lv-steam {
  opacity: 0;
  transform-box: fill-box;
  transform-origin: 50% 100%;
  animation: wp-steam calc(var(--beat) * 2) cubic-bezier(.16, .72, .38, 1) infinite;
  animation-delay: var(--d, 0s);
}
@keyframes wp-steam {
  0% { opacity: 0; transform: translateY(2px) scale(.6); }
  30% { opacity: .7; }
  100% { opacity: 0; transform: translateY(-9px) scale(1.15); }
}
.wp-lv-lamp {
  opacity: .3;
  animation: wp-lamp calc(var(--beat) * 4) cubic-bezier(.45, .05, .55, .95) infinite;
  animation-delay: var(--d, 0s);
}
@keyframes wp-lamp { 0%, 100% { opacity: .22; } 50% { opacity: .46; } }
.wp-lv-fan {
  transform-box: fill-box;
  transform-origin: 50% 50%;
  animation: wp-fan calc(var(--beat) / 2) linear infinite;
}
@keyframes wp-fan { to { transform: rotate(360deg); } }

/* A door is something that HAPPENS. The leaf is shut until the engine sees somebody within a
   tile and a half of it, which makes walking between rooms legible from across the building. */
.wp-leaf {
  transform-box: fill-box;
  transform-origin: 0% 50%;
  transition: transform 260ms cubic-bezier(.34, 1.4, .64, 1);
}
.wp-leaf.is-open { transform: scaleX(.18); }

/* Labels hold a constant SCREEN size while the world zooms under them — the standard answer for
   anything that is read rather than looked at, and the reason the same 132px bubble does not turn
   into a 264px banner the moment the camera moves in. The world scales; the words do not. */
.wp-plaque {
  position: absolute;
  z-index: 300;
  transform: scale(var(--inv, 1));
  transform-origin: 0 0;
  /* Held at constant SCREEN size, so its layout box is its pixel box — which makes the cap on
     its width computable: the room's own width on screen, tiles by the tile size by the zoom.
     A flat cap in world pixels was right at one scale and wrong at every other, and pulled back
     it let three department signs sit on top of each other over rooms 64px wide. */
  max-width: calc(var(--rw, 8) * 32px * var(--z, 1));
  font-size: 7px;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: .04em;
  text-transform: uppercase;
  font-weight: 700;
  color: var(--wp-bg);
  background: var(--wp-plate);
  padding: 1px 4px;
  box-shadow: 1px 1px 0 var(--wp-ink);
  white-space: nowrap;
  pointer-events: none;
}

/* ── a character ───────────────────────────────────────────────────────────────────────────
   The element is a POINT — a zero-sized anchor standing on a tile — and everything hangs off it.
   That is what lets the engine move a person with one transform, and what stops the text around
   somebody from deciding where they are allowed to be. */

.wp-cast { position: absolute; inset: 0; }
.wp-actor {
  position: absolute;
  left: 0;
  top: 0;
  width: 0;
  height: 0;
  z-index: 100;
  cursor: pointer;
  --c-shirt: var(--accent, var(--wp-h1));
  /* The one focal role mark on the chest. LIGHT, like a card on a lanyard — mixed toward the
     ink it sat in a hole punched out of the shirt; a badge is worn ON the cloth. */
  --c-badge: color-mix(in srgb, var(--accent, var(--wp-h1)) 45%, #f3efe4);
  --c-eye: #1b1723;
  --c-mouth: color-mix(in srgb, var(--c-skin, #e0a877) 62%, var(--wp-ink));
  /* A VISITOR, not a hole. One flat tone for every part of a human silhouette is what a renderer
     draws when it cannot find a sprite, so a foreign run read as broken rather than as a
     category — correctly, because the information the eye is missing there is not hue but VALUE
     STRUCTURE. This is a five-step ramp like everybody else's, in ONE hue: the same
     --vscode-charts-purple the rail already prints NOT OURS in, so the two surfaces agree
     about who this is. A monochrome person in a colour photograph. */
  --c-guest: var(--vscode-charts-purple, #b180d7);
  --c-guest-hair: color-mix(in srgb, var(--c-guest) 46%, var(--wp-ink));
  --c-guest-skin: color-mix(in srgb, var(--c-guest) 40%, var(--wp-fg));
  /* The COAT is the largest area a visitor puts on screen, so it decides whether they read as
     background or as the loudest thing in the room. At 62% it out-saturated our own team, which
     inverts the meaning: someone else's session should be legible and unmistakable, never the
     first thing the eye lands on. The internal VALUE range does the identifying work; the
     saturation only has to be enough to say "one hue". */
  --c-guest-coat: color-mix(in srgb, var(--c-guest) 46%, var(--wp-bg));
  --c-guest-leg: color-mix(in srgb, var(--c-guest) 32%, var(--wp-bg));
  --c-guest-boot: color-mix(in srgb, var(--c-guest) 30%, var(--wp-ink));
  --c-guest-eye: color-mix(in srgb, var(--c-guest) 30%, var(--wp-ink));
  --c-guest-ink: color-mix(in srgb, var(--c-guest) 34%, var(--wp-ink));
  --c-ghost: var(--c-guest-coat);
  --c-ghost-ink: var(--c-guest-ink);
}

.wp-body {
  position: absolute;
  left: 0;
  bottom: 0;
  width: 32px;
  height: 32px;
  --body-scale: 1.3;
  transform: translateX(-50%) scale(var(--body-scale)) rotate(var(--lean, 0deg));
  transform-origin: 50% 100%;
}
/* A HIT AREA BIGGER THAN THE PERSON. Pulled back, a body is ~11px of drawn sprite and desks sit
   three tiles apart — an aimed click landed on the NEIGHBOUR (measured: 3 of 4). The pad is
   invisible, sits behind the art, and stops short of half the desk pitch so it can never steal
   a click meant for the person next door. */
.wp-body::after {
  content: "";
  position: absolute;
  left: 50%; top: 50%;
  width: 40px; height: 40px;
  transform: translate(-50%, -50%);
  border-radius: 6px;
  z-index: -1;
}
/* THE DOLL. One composed drawing per person; posture and gait are the ELEMENT's. Every verb
   this sprite has composes through custom properties on ONE transform — the face flip, the walk
   waddle, the seated tuck, the lounge roll — because two rules writing the transform on the
   same element silently erase each other. */
.wp-body .wp-doll {
  position: absolute;
  left: 0;
  bottom: 0;
  transform: scaleX(var(--fx, 1)) translate(var(--wx, 0px), var(--wy, 0px)) rotate(var(--wr, 0deg));
  transform-origin: 50% 100%;
}
.wp-actor.face-left .wp-body .wp-doll { --fx: -1; }

/* Seated: tucked up toward the desk it works at, so body and workstation read as one station. */
.wp-actor[data-posture="sit"]:not(.is-walking) .wp-body .wp-doll,
.wp-actor[data-posture="slump"]:not(.is-walking) .wp-body .wp-doll { --wy: -4px; }
/* FINISHED lies down: the one posture change that reads at every rung is the aspect flip, so
   the figure ROLLS onto the couch. A quarter turn keeps every source pixel square. */
.wp-actor[data-posture="lounge"]:not(.is-walking) .wp-body .wp-doll {
  --wr: 90deg;
  --wy: -3px;
  --wx: 2px;
}

/* THE WALK is a rocking waddle on the engine's own footfall counter — the roguelike gait for
   front-facing art. The first cut flipped ±5° on EVERY footfall: at 3.6 tiles/s that is a 7Hz
   wobble, faster than an eye (or a video model — it reported "static sprites gliding") can
   resolve, so the gait has to live at HALF the footfall rate: lean left across one stride, lean
   right across the next, with the dip on each passing beat. ~1.8Hz at full pace, and the lean is
   wide enough to read at a glance. Driven by data-step so it stays keyed to DISTANCE walked. */
.wp-actor.is-walking[data-step="0"] .wp-body .wp-doll { --wr: -10deg; --wx: -1px; }
.wp-actor.is-walking[data-step="1"] .wp-body .wp-doll { --wr: -5deg; --wy: 2px; }
.wp-actor.is-walking[data-step="2"] .wp-body .wp-doll { --wr: 10deg; --wx: 1px; }
.wp-actor.is-walking[data-step="3"] .wp-body .wp-doll { --wr: 5deg; --wy: 2px; }

/* The head of the company stands taller. The cheapest true thing the picture can say about the
   one agent everybody else reports to, and it needs no label to say it. 1.5, not 1.3: at 2x a
   half step keeps the brain's source pixels a whole three device pixels wide. */
.wp-actor.is-brain .wp-body { --body-scale: 1.65; }
.wp-body::before {
  content: "";
  position: absolute;
  z-index: 4;
  pointer-events: none;
  opacity: .9;
}
/* Relationship lives on the silhouette, not in a seven-pixel label or a pod colour. The root's
   diamond reads as one independent origin; the child's open corner is a branch. Both survive a
   pulled-back camera because their edges grow with the body rather than counter-scaling as UI. */
.wp-actor[data-lineage="root"] .wp-body::before {
  width: 7px;
  height: 7px;
  left: 12px;
  top: -8px;
  background: var(--wp-fg);
  transform: rotate(45deg);
  box-shadow: 1px 1px 0 var(--wp-ink);
}
.wp-actor[data-lineage="child"] .wp-body::before {
  width: 8px;
  height: 8px;
  left: -4px;
  top: 3px;
  border-left: 3px solid var(--wp-fg);
  border-bottom: 3px solid var(--wp-fg);
  filter: drop-shadow(1px 1px 0 var(--wp-ink));
}
.wp-actor.is-brain::before {
  content: "";
  position: absolute;
  left: -22px;
  bottom: -5px;
  width: 44px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid color-mix(in srgb, var(--wp-h3) 76%, transparent);
  animation: wp-crown calc(var(--beat) * 2) ease-in-out var(--d, 0s) infinite;
}
@keyframes wp-crown {
  0%, 100% { opacity: .35; transform: scale(.9); }
  50% { opacity: .9; transform: scale(1.08); }
}

.wp-shade {
  /* A border-radius 50% ellipse is a SOFT shape in a scene made entirely of hard ones, and at
     sprite scale it read as a smudge rather than as contact with the floor. Stepped in threes it
     is a pixel shape.
     It also has to come OUT from under the body: at 26px under a 36px sprite it was entirely
     hidden behind the character it belonged to, which is why the floor still looked like a floor
     nobody was standing on. Thrown down and to the right, the same way every wall and every prop
     in this building throws. */
  position: absolute;
  left: -10px;
  bottom: -4px;
  width: 26px;
  height: 7px;
  background: var(--wp-foot);
  /* A cast shadow is a drawing, never a target. It is 32px wide against a 36px body and it sits
     down and to the RIGHT of the person it belongs to, so it overhangs the neighbour's tool
     glyphs — and it was catching their clicks: a pointer at a glyph's own centre resolved to the
     shadow of the person standing behind. Two of the sixteen actors on screen, every frame. */
  pointer-events: none;
  clip-path: polygon(
    6px 0, 24px 0, 24px 2px, 28px 2px, 28px 6px, 24px 6px, 24px 8px,
    6px 8px, 6px 6px, 2px 6px, 2px 2px, 6px 2px);
}
.wp-actor.is-brain .wp-shade { width: 34px; left: -14px; }
/* A seated body's contact with the floor is its feet and the seat under it, not a standing
   footprint — narrower, and pulled back under the chair rather than out in front of it. */
.wp-actor[data-posture="sit"] .wp-shade,
.wp-actor[data-posture="slump"] .wp-shade { width: 22px; left: -8px; bottom: -2px; }
.wp-actor[data-posture="lounge"] .wp-shade { width: 30px; left: -12px; bottom: -2px; }

/* The wave hand still runs on the two-frame flip. */
.wp-f { opacity: 0; }
/* The old pair, kept for the wave hand. */
@keyframes wp-fa { 0%, 62% { opacity: 1; } 63%, 100% { opacity: 0; } }
@keyframes wp-fb { 0%, 62% { opacity: 0; } 63%, 100% { opacity: 1; } }
/* A body off the ground throws a smaller, tighter shadow. */
.wp-actor.is-walking .wp-shade { transform: scaleX(.82); }


/* EVERYONE WEARS THEIR NAME. Resting plates used to hide until hover — the reasoned worry was
   an ops-board of thirty plates — but the owner asked for the opposite in his own words ("Can
   we show the names of the agents in the game ?"), and a company you watch is a company you can
   READ: who is who, at a glance, like any staffed floor in a sim. The calm is kept by WEIGHT,
   not absence — a resting plate is dimmed; a working, erroring or asking plate is full. */
.wp-actor[data-attention="finished"] .wp-tag,
.wp-actor[data-attention="ready"] .wp-tag,
.wp-actor[data-attention="not-ours"] .wp-tag { opacity: .72; }
.wp-actor[data-attention="finished"]:hover .wp-tag,
.wp-actor[data-attention="ready"]:hover .wp-tag,
.wp-actor[data-attention="not-ours"]:hover .wp-tag,
.wp-actor[data-attention="finished"].is-picked .wp-tag,
.wp-actor[data-attention="ready"].is-picked .wp-tag,
.wp-actor[data-attention="not-ours"].is-picked .wp-tag { opacity: 1; }

.wp-tag {
  position: absolute;
  /* The engine writes the edge clamp onto this, in WORLD pixels — a nameplate scales with the
     floor, unlike a speech bubble, so its correction is a world distance. On the left property
     rather than in the transform for the same reason the bubble's is: a transitioned property
     animates the correction away before anybody sees it. */
  left: 0;
  top: 3px;
  transform: translateX(-50%);
  display: block;
  /* Seats are three tiles apart, i.e. seventy-two world pixels. A plate allowed seventy-six was
     wider than the space between two people by construction, so two neighbours' names overlapped
     before anything else in the scene did. */
  max-width: 66px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 7px;
  line-height: 1.35;
  letter-spacing: .01em;
  white-space: nowrap;
  color: var(--wp-fg);
  /* Opaque, not translucent. Two characters standing a tile apart WILL overlap — that is what a
     crowd is — so the one in front has to cover the one behind cleanly. */
  background: color-mix(in srgb, var(--wp-bg) 92%, var(--wp-fg));
  padding: 0 3px;
  border-bottom: 1px solid var(--accent, var(--wp-h1));
  pointer-events: none;
}
.wp-actor:hover .wp-tag,
.wp-actor:focus-visible .wp-tag { max-width: none; overflow: visible; }
.wp-tag i { font-style: normal; color: var(--wp-dim); margin-left: 3px; }
.wp-actor.is-brain .wp-tag { font-weight: 700; border-bottom-color: var(--wp-h3); }
.wp-actor[data-lineage="root"] .wp-tag::before { content: "\25A0"; margin-right: 3px; }
.wp-actor[data-lineage="child"] .wp-tag::before { content: "\2514"; margin-right: 3px; }
.wp-actor[data-lineage="child"] .wp-tag { opacity: 0; }
.wp-actor[data-lineage="child"]:hover .wp-tag,
.wp-actor[data-lineage="child"]:focus-visible .wp-tag,
.wp-actor[data-lineage="child"].is-picked .wp-tag { opacity: 1; }

/* What this one can DO, read off its own definition file.
   Six marks at twelve CSS pixels — which the old whole-building fit then HALVED to six — was
   measured as illegible, and illegible is not ergonomic. So it is the three that make this agent
   different from everybody else, at fourteen pixels, on a camera that no longer shrinks the world
   to fit the panel. Hung BESIDE the body rather than under the name: as a row it was half again
   the character's own width and the loudest thing on them, which is the chrome-crowds-the-sprite
   complaint in miniature. The full set, with its words, is one hover away. */
.wp-can {
  /* TOOLS ON THE DESK, not a column of chips.
     Three framed squares stacked beside a 36x54 character measured 32px each on screen — ninety-six
     pixels of interface against fifty-four pixels of person, so the loudest object in a room full
     of people was a stack of icons. Laid flat at the feet, with the plate taken away and the same
     hard pixel shadow every sign in this building throws, they stop being interface and become
     things lying on the desk. Same size, a quarter of the weight. */
  position: absolute;
  left: 0;
  top: var(--belt, 15px);
  transform: translateX(-50%);
  display: flex;
  gap: 2px;
  /* HOVER-ONLY. Three framed marks under every one of forty-six people was a floor of tiny
     buttons; the tools now come out when the pointer meets the person, exactly like the full
     kit. Opacity rather than display, so the reveal can fade and the layout never jumps. */
  opacity: 0;
  pointer-events: none;
  transition: opacity 160ms ease;
}
.wp-actor:hover .wp-can,
.wp-actor:focus-visible .wp-can,
.wp-actor.is-met .wp-can {
  opacity: 1;
  pointer-events: auto;
}
/* The back rank has no nameplate under its boots, so its belt sits right at them.
   Tried and REJECTED: lifting it above the head, the way the nameplate is lifted. It clears the
   front rank's placard and it reads as three icons floating unattached over somebody — tools that
   have stopped belonging to anyone. The two measured overlaps that remain are a back-rank belt
   under a front-rank stamp, where depth ordering already draws the stamp on top; that reads as one
   person standing in front of another, which is what it is. */
.wp-actor[data-label="up"] .wp-can { --belt: 2px; }
.wp-fac {
  --mark: color-mix(in srgb, var(--accent, var(--wp-h1)) 34%, #f4f1ff);
  font-style: normal;
  display: inline-flex;
  /* Keeps the mark at sixteen world pixels, i.e. the thirty-two on screen an earlier round
     measured as the point where these stop being illegible. Dropping the plate must not quietly
     shrink them back. */
  padding: 1px;
  filter: drop-shadow(1px 1px 0 var(--wp-ink)) drop-shadow(-1px 0 0 var(--wp-ink))
    drop-shadow(0 -1px 0 var(--wp-ink));
  transition: transform 150ms cubic-bezier(.34, 1.56, .64, 1);
}
.wp-fac:hover,
.wp-fac:focus-visible {
  --mark: #fff;
  transform: translateY(-4px) scale(1.15);
  outline: none;
}
.wp-fac:active { transform: translateY(-1px); }
.wp-fac svg { display: block; }
.wp-actor[data-status="foreign"] .wp-fac,
.wp-actor[data-status="done"] .wp-fac { background: color-mix(in srgb, var(--wp-dim) 70%, var(--wp-bg)); }

/* The whole kit with its words, for a reader who points at somebody. Display NONE at rest, not
   opacity zero: an invisible element that still occupies layout is how a label-collision check
   once reported four collisions nobody could see. */
.wp-kit { display: none; }
.wp-actor:hover .wp-kit,
.wp-actor:focus-visible .wp-kit {
  display: block;
  position: absolute;
  left: 10px;
  top: 34px;
  z-index: 950;
  padding: 3px 5px;
  background: var(--wp-chrome);
  border: 1px solid var(--wp-line);
  box-shadow: 2px 2px 0 var(--wp-ink);
  font-size: 8px;
  line-height: 1.5;
  color: var(--wp-fg);
  white-space: nowrap;
  pointer-events: none;
}
.wp-kit i { font-style: normal; display: flex; align-items: center; gap: 4px; --mark: var(--wp-fg); }

/* The line above their head.
   "A lot of transparency" was the ask, and the answer used to be opacity zero until you hovered
   the exact right character — the richest information in the product, invisible unless you knew
   where to point. It is on at rest now. What makes that possible is the camera: eight people on
   screen instead of twenty-five, so the engine can hand out three levels of bubble and drop any
   that would collide with another bubble or with somebody's name. */
.wp-say {
  position: absolute;
  /* THE EDGE CLAMP RIDES THE LEFT PROPERTY, NOT THE TRANSFORM, for a measured reason rather
     than a stylistic one. The transform carries a 220ms transition, so a clamp written into it is ANIMATED
     to — for a fifth of a second after a bubble appears or the camera moves, the element sits at
     the position the clamp exists to prevent. The probe caught exactly that: --sx correct at
     69px, the computed matrix still reading the unshifted -66, and sixty-three pixels of the line
     outside the panel. The left property is not transitioned, so it lands on the frame it is set.
     Multiplied by --inv because left is in the STAGE coordinate space, which the camera
     scales, while the clamp is a distance on the SCREEN. */
  left: calc(var(--sx, 0px) * var(--inv, 1));
  bottom: 44px;
  transform: translate(-50%, 3px) scale(var(--inv, 1));
  transform-origin: 50% 100%;
  width: 132px;
  padding: 2px 5px;
  font-size: 9px;
  line-height: 1.3;
  color: var(--wp-ink);
  background: var(--wp-paper);
  box-shadow: 2px 2px 0 var(--wp-ink);
  opacity: 0;
  pointer-events: none;
  /* Opacity only. Transitioning the transform also transitions the scale(--inv) the camera
     writes, so every zoom step left the bubbles a fifth of a second behind the floor they belong
     to — a label that lags the world it labels. */
  transition: opacity 220ms ease;
  z-index: 5;
}
.wp-say b { font-weight: 400; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wp-say::after {
  content: "";
  position: absolute;
  left: 50%;
  bottom: -4px;
  width: 4px;
  height: 4px;
  margin-left: -2px;
  background: var(--wp-paper);
}
.wp-actor.is-saying .wp-say {
  opacity: 1;
  transform: translate(-50%, 0) scale(var(--inv, 1));
}
.wp-actor:hover, .wp-actor:focus-visible { z-index: 900 !important; }

/* The back rank wears its name over its head: three tiles is room for two people and not enough
   for two people plus their labels, and the front body would otherwise be drawn over the back
   body's name. */
/* EVERY OFFSET THAT HANGS OFF A HEAD IS DERIVED FROM THE HEAD, never from a constant.
   These were four numbers tuned for the one sprite height there used to be. A seated body is
   twelve pixels shorter, so each of them left its label floating half a tile above the person it
   belongs to — and the speech layout's reserved rectangles, which are the only reason two labels
   are never drawn over each other, were computed from the same stale constants. --head is set per
   actor from status.ts HEAD, and the layout reads the identical number off data-head. */
.wp-actor[data-label="up"] .wp-tag { top: auto; bottom: calc(var(--head, 32px) + 4px); }
.wp-actor[data-label="up"] .wp-say { bottom: calc(var(--head, 32px) + 40px); }
.wp-actor[data-label="up"] .wp-mark { bottom: calc(var(--head, 32px) + 28px); }
.wp-actor[data-label="up"] .wp-kit { top: auto; bottom: calc(var(--head, 32px) + 44px); }

.wp-mark {
  position: absolute;
  left: 0;
  bottom: var(--head, 32px);
  transform: translateX(-50%) scale(.5);
  transform-origin: 50% 100%;
  pointer-events: none;
}
/* The z's belong over the HEAD, so they follow it down when somebody sits. */
.wp-zzz { position: absolute; left: 14px; bottom: calc(var(--head, 32px) - 6px); --mark: var(--wp-dim); }

/* Somebody has come over to say something. Both of them stop and turn to each other — a message
   that lands with nobody reacting is a note flying past a person rather than to one. */
.wp-actor.is-talking .wp-body { animation: wp-talk 480ms ease-in-out infinite; }
@keyframes wp-talk {
  0%, 100% { transform: translateX(-50%) scale(var(--body-scale)) translateY(0); }
  50% { transform: translateX(-50%) scale(var(--body-scale)) translateY(-2px); }
}
.wp-actor.has-post .wp-shade { background: color-mix(in srgb, var(--wp-h3) 60%, transparent); }

/* A RUNNING worker the registry says has stopped stops. Scoped to the sprite, never to the whole
   actor: an ancestor opacity composites the nameplate and the stamp with it, and a greyed-out
   ERROR is the one word that must never be hard to read. */
.wp-actor[data-stalled="1"] .wp-body { filter: saturate(.7) brightness(.94); }
/* NEVER opacity. A translucent person reads as a rendering bug, not a state — the done-fade was
   removed for exactly that ("Agents are also transparent kind of... I don't know why"), and a
   stalled body fading by the same mechanism is the same defect waiting for the next held agent. */
/* A FINISHED worker is NOT translucent either — a real registry is mostly finished runs, so the
   fade made most of the building ghostly and nothing on screen explained why. The body already
   SITS at the rest end of its room, which is the whole signal; the sprite only cools a touch, at
   full opacity. */
.wp-actor[data-status="done"] .wp-body { filter: saturate(.82) brightness(.97); }
/* NOT faded with idle either. A visitor's session is not ours to age, and fading a body that is
   already drawn in one hue is precisely how it stopped reading as a person and started reading as
   a gap in the picture. Same treatment as finished: present, merely quieter. */
.wp-actor[data-status="foreign"] .wp-body { filter: saturate(.82) brightness(.97); }
.wp-actor[data-status="error"] .wp-body { filter: drop-shadow(0 0 4px color-mix(in srgb, var(--wp-bad) 70%, transparent)); }

.wp-proto { position: absolute; visibility: hidden; pointer-events: none; }

/* ── presence: the world notices the pointer ─────────────────────────────────────────────────
   Three grants, each earned by the hand at the glass and given by the engine as a class. The
   pieces themselves are pixel art from the sheet; nothing here is a CSS shape. */

/* The claim ring: picked up like a unit. Under the body, over its contact shade. */
.wp-ring { position: absolute; left: -18px; bottom: -7px; display: none; pointer-events: none; }
/* The same hard pixel shadow every sign in the building carries — it is what keeps one line of
   accent legible on any floor it lands on. */
.wp-ring svg { filter: drop-shadow(1px 1px 0 var(--wp-ink)); }
.wp-actor.is-picked .wp-ring { display: block; animation: wp-claim calc(var(--beat)) ease-in-out infinite; }
@keyframes wp-claim { 0%, 100% { opacity: .9; } 50% { opacity: .45; } }

/* The greeting: hovered, a character perks up and waves with its own hand. The hand rides the
   shoulder and swings on the sprite system's own two-frame flip. */
.wp-hi { position: absolute; left: 13px; bottom: calc(var(--head, 32px) - 8px); display: none; pointer-events: none; }
.wp-actor.is-met .wp-hi { display: block; }
.wp-hi .wp-f0 { opacity: 1; }
.wp-hi .wp-f1 { opacity: 0; }
.wp-actor.is-met .wp-hi .wp-f0 { animation: wp-fa calc(var(--beat) / 6) steps(1, end) infinite; }
.wp-actor.is-met .wp-hi .wp-f1 { animation: wp-fb calc(var(--beat) / 6) steps(1, end) infinite; }
.wp-actor.is-met .wp-body { animation: wp-greet calc(var(--beat) / 3) steps(2, end) infinite; }
@keyframes wp-greet {
  0%, 100% { transform: translateX(-50%) translateY(0) rotate(var(--lean, 0deg)); }
  50% { transform: translateX(-50%) translateY(-2px) rotate(var(--lean, 0deg)); }
}
.wp-actor.is-met .wp-tag { color: var(--wp-fg); }

/* The poke: a click LANDS before it opens anything — squash, hop, and a startled mark, the
   cheapest honest dopamine a tile game has. One-shot; the engine re-arms it per click. */
.wp-actor.is-poked .wp-body { animation: wp-poke 560ms cubic-bezier(.34, 1.56, .64, 1) 1; }
@keyframes wp-poke {
  0% { transform: translateX(-50%) rotate(var(--lean, 0deg)) scale(1, 1); }
  22% { transform: translateX(-50%) rotate(var(--lean, 0deg)) scale(1.14, .8); }
  55% { transform: translateX(-50%) translateY(-6px) rotate(var(--lean, 0deg)) scale(.94, 1.08); }
  100% { transform: translateX(-50%) rotate(var(--lean, 0deg)) scale(1, 1); }
}
.wp-bang { position: absolute; left: 16px; bottom: calc(var(--head, 32px) + 4px); display: none; pointer-events: none; }
.wp-actor.is-poked .wp-bang { display: block; animation: wp-bang 760ms steps(2, end) 1 both; }
@keyframes wp-bang {
  0% { opacity: 0; transform: translateY(4px); }
  30% { opacity: 1; transform: translateY(0); }
  85% { opacity: 1; transform: translateY(0); }
  100% { opacity: 0; transform: translateY(0); }
}

${STAMP_CSS}

/* ── reduced motion ────────────────────────────────────────────────────────────────────────
   Everything above degrades to a still building with everybody standing at their own desk. The
   engine never starts its clock, so nothing walks and nothing pulses; the camera holds where it
   was put, and the speech stays ON, because a reader who has turned motion off still needs to
   know what the team is doing. */
@media (prefers-reduced-motion: reduce) {
  .wp-view::after,
  .wp-view::before,
  .wp-core,
  .wp-lv-screen,
  .wp-lv-steam,
  .wp-lv-lamp,
  .wp-lv-fan,
  .wp-actor.is-brain::before,
  .wp-actor.is-talking .wp-body,
  .wp-actor.is-met .wp-body,
  .wp-actor.is-poked .wp-body,
  .wp-actor.is-poked .wp-bang,
  .wp-actor.is-picked .wp-ring,
  .wp-actor.is-met .wp-hi .wp-f0,
  .wp-actor.is-met .wp-hi .wp-f1,
  .wp-view[data-follow="0"] .wp-cam-follow { animation: none !important; }
  /* Still noticed, just still: the raised hand, the mark and the ring hold without moving. */
  .wp-hi .wp-f0 { opacity: 1; }
  .wp-hi .wp-f1 { opacity: 0; }
  .wp-actor.is-poked .wp-bang { opacity: 1; }
  .wp-pool, .wp-shut, .wp-leaf, .wp-rungs i, .wp-fac, .wp-can { transition: none; }
  .wp-lit .wp-pool { animation: none !important; }
  .wp-lv-steam { opacity: 0; }
}

body.vscode-reduce-motion .wp-view::after,
body.vscode-reduce-motion .wp-view::before,
body.vscode-reduce-motion .wp-core,
body.vscode-reduce-motion .wp-lv-screen,
body.vscode-reduce-motion .wp-lv-steam,
body.vscode-reduce-motion .wp-lv-lamp,
body.vscode-reduce-motion .wp-lv-fan,
body.vscode-reduce-motion .wp-actor.is-brain::before,
body.vscode-reduce-motion .wp-actor.is-talking .wp-body,
body.vscode-reduce-motion .wp-actor.is-met .wp-body,
body.vscode-reduce-motion .wp-actor.is-poked .wp-body,
body.vscode-reduce-motion .wp-actor.is-poked .wp-bang,
body.vscode-reduce-motion .wp-actor.is-picked .wp-ring,
body.vscode-reduce-motion .wp-actor.is-met .wp-hi .wp-f0,
body.vscode-reduce-motion .wp-actor.is-met .wp-hi .wp-f1,
body.vscode-reduce-motion .wp-view[data-follow="0"] .wp-cam-follow,
body.vscode-reduce-motion .wp-lit .wp-pool { animation: none !important; }
body.vscode-reduce-motion .wp-pool,
body.vscode-reduce-motion .wp-shut,
body.vscode-reduce-motion .wp-leaf,
body.vscode-reduce-motion .wp-rungs i,
body.vscode-reduce-motion .wp-fac,
body.vscode-reduce-motion .wp-can { transition: none; }
body.vscode-reduce-motion .wp-lv-steam { opacity: 0; }
`;
