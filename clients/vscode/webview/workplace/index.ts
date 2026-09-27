/** The workplace view — one entry point.
 *
 *      renderWorkplace(state, nonce) -> a complete HTML document
 *
 *  Hand it a TeamState and the webview's nonce; assign the result to webview.html. Nothing else
 *  needed: art (data-URI atlas of the CC0 Kenney tiles), stylesheet and the one script are all
 *  generated into the document — no bundle to build, no asset through asWebviewUri, no request
 *  that could leave the machine (CSP allows img-src data: and nothing else).
 *
 *  Render ONCE, then keep it — the engine is a running simulation with its own clock, so update
 *  by posting the next scene, not by reassigning the document:
 *
 *      panel.webview.html = renderWorkplace(state, nonce)                     // once
 *      panel.webview.postMessage({ type: "team", html: renderActors(state) }) // every refresh
 *
 *  The engine swaps #wp-mount, re-binds every body by data-run-id, and carries positions and
 *  journeys across the swap — someone sent to another room is watched the whole way there.
 *  Reassigning .html still works (the document recovers the last snapshot from setState) but
 *  restarts the clock each time; see motion.ts.
 */
import type { TeamState } from "../../src/team";
import { renderScene } from "./scene";
import { STYLE } from "./style";
import { SCRIPT } from "./motion";

export { renderScene, renderActors, brainOf, deptsOf, worldFor, placeOf } from "./scene";
export type { Cast } from "./scene";
export { STYLE } from "./style";
// Status lexicon is public surface, not a private helper: the side bar imports it, and anything
// naming a run's state must reuse these words, never invent a second set.
export { STAMPS, STAMP_CSS, STALL_SECONDS, WORDS, isHeld, stampFor, stampHtml } from "./status";
export type { Stamp, StampKind } from "./status";

/** A nonce lands inside an attribute and a CSP header value, so it's reduced to the alphabet a
 *  nonce allows rather than trusted. A strange caller value breaks the script, never the
 *  document. */
function safeNonce(nonce: string): string {
  const clean = String(nonce).replace(/[^A-Za-z0-9+/=_-]/g, "");
  return clean || "workplace";
}

/** An embedded panel rendered BESIDE the room — the roster, supplied by the host.
 *
 *  "Your team and agents panel are still on the left side, whereas they should be in the big main
 *  panel somewhere." Room and roster are two views of one company and belong on the same wide
 *  surface; the side bar is left for the one conversation in progress. Host owns the roster's
 *  markup, so this module keeps knowing only about the world.
 */
export interface WorkplaceAside {
  style: string;
  body: string;
  script: string;
}

export function renderWorkplace(state: TeamState, nonce: string, aside?: WorkplaceAside): string {
  const n = safeNonce(nonce);
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src data:; font-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${n}';">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The team</title>
<style>${STYLE}</style>
${aside ? `<style>${aside.style}
/* Company, two ways, one surface: room left, roster right. Roster scrolls on its own so a long
   team never pushes the room off screen. */
.wp-split { display: flex; align-items: stretch; height: 100vh; width: 100%; position: relative;
  container-type: inline-size; }
.wp-split > .wp-list {
  flex: 1 1 auto; min-width: 0; overflow-y: auto; overflow-x: hidden;
  background: var(--vscode-editor-background);
}
/* Scene sizes itself to the VIEWPORT when it owns the page; inside the split it must size to its
   half. Stacked layout measured: room ran 894px tall in a 495px slot, its bottom-right survey
   controls landing ON TOP of the roster — eating clicks, and a click there scrubbed the zoom. */
.wp-split .wp { height: 100%; }
/* A second rule used to live here too: a fixed 240-380px sidebar width from when a world shared
   the panel. Equal specificity meant it won — roster got 367px against 1045px empty at full
   width, and the 560px container query was permanently true, so the table never rendered as a
   table. One selector, one owner of the width. */
/* ONE owner per fact on the shared panel: rail's header states scope and counts, so the world's
   HUD drops its duplicate tally and project sub-line here. Standalone, it keeps them — it's the
   only header in the room there. */
.wp-split .wp-hud .wp-tally, .wp-split .wp-hud .wp-sign-sub { display: none; }
/* 560, not 720 — breakpoint applies to the EDITOR GROUP, not the window. Measured live: a
   1280px laptop with both side bars open leaves ~630px here, so 720px stacked the split on most
   real windows, only flipping side-by-side above ~1440px. At 560 the roster keeps its 240px
   floor and the room keeps ~320px — a room, not a slot. */
/* No narrow regime any more: with no room beside it, the roster fills the panel at every width
   — never hides, overlays, or needs a toggle. */

</style>` : ""}
</head>
<body>
${aside
  // The roster is the "Team tab" users called laggy: "remove the game like features... orient it
  // more like we're doing in the web." The ~9,700-line tile world (world/sim/scene/tiles) was the
  // cause — 149% CPU, 2.99GB RSS, a CDP round-trip missing a 60s timeout at full width, the
  // 48-actor frame-budget test failing 23.7ms vs a 20ms budget. renderScene is no longer called
  // here, so the simulation never starts — that alone is the fix; the roster itself was never
  // slow. Scene modules stay (reversible); deleting their 4,300 lines is a separate call.
  //
  // .wp-list is the REPAINT HOST: railScript opens every roster update with
  // querySelector(".wp-list") and bails when it's absent. Removing it silently killed every
  // repaint — live updates, drill-in, the view chooser. It stays, now wrapping the roster instead
  // of sitting beside a room.
  ? `<div class="wp-split"><div class="wp-list">${aside.body}</div></div>`
  : renderScene(state)}
<script nonce="${n}">${SCRIPT}</script>
${aside ? `<script nonce="${n}">${aside.script}</script>` : ""}

</body>
</html>`;
}
