/** The seam between the workplace DATA and the workplace VISUAL.
 *
 *  The pixel-art room is built separately under webview/workplace/ — a visual is reworked far
 *  more often than the data behind it, and keeping them apart means either can change alone.
 *  This module is the adapter: it takes a TeamState, hands it to whichever renderer is present,
 *  and falls back to a plain, honest room so the pipeline is testable before the art exists.
 */
import { ZONES, leads, reportsOf } from "./team";
import type { TeamState, Worker } from "./team";

/** The pixel-art renderer, loaded once. Bundled from webview/workplace/ by npm run
 *  build:workplace and required at runtime rather than imported: it lives outside tsc's
 *  rootDir on purpose, so the visual can be reworked without recompiling the data layer. */
type Aside = { style: string; body: string; script: string };
let art: ((state: TeamState, nonce: string, aside?: Aside) => string) | null | undefined;
/** Just the scene, for pushing an update into a document that is already live. */
let scene: ((state: TeamState) => string) | null | undefined;

export function renderWorkplace(
  state: TeamState,
  nonce: string,
  log?: { appendLine(line: string): void },
  /** The roster, rendered beside the room. Optional so the plain fallback below still works. */
  aside?: Aside,
): string {
  if (art === undefined) {
    try {
      art = require("./workplace.js").renderWorkplace;
    } catch (err) {
      // Reported, not swallowed: a bundle that fails to load would otherwise degrade silently on
      // every render for the life of the session, with nothing anywhere saying why.
      log?.appendLine(`workplace art unavailable, using the plain room: ${err}`);
      art = null;
    }
  }
  if (art) {
    try {
      return art(state, nonce, aside);
    } catch (err) {
      log?.appendLine(`workplace art failed to render: ${err}`);
    }
  }
  // A missing or broken renderer degrades to a plain room rather than an empty panel: the team is
  // still legible, just not beautiful.
  return plainRoom(state, nonce);
}

/** Escaped once, here: a worker's name and activity are agent output. */
function esc(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function worker(w: Worker, all: Worker[]): string {
  const reports = reportsOf(all, w.run_id);
  const nested = reports.length
    ? `<div class="reports">${reports.map((r) => worker(r, all)).join("")}</div>`
    : "";
  return `<div class="worker ${esc(w.status)}" data-run-id="${esc(w.run_id)}">
    <div class="who">${esc(w.name)}</div>
    <div class="doing">${esc(w.activity)}</div>
    ${nested}
  </div>`;
}

export function plainRoom(state: TeamState, nonce: string): string {
  const rooms = ZONES.map((zone) => {
    const here = leads(state.workers).filter((w) => w.zone === zone.id);
    return `<section class="zone"><h2>${esc(zone.label)}</h2>
      ${here.map((w) => worker(w, state.workers)).join("") || '<p class="empty">—</p>'}
    </section>`;
  }).join("");
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { margin: 0; padding: 1rem; font-family: var(--vscode-font-family);
         color: var(--vscode-foreground); background: var(--vscode-editor-background); }
  .rooms { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 1rem; }
  .zone { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: .6rem; }
  h2 { margin: 0 0 .5rem; font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
       color: color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 70%, var(--vscode-editor-foreground, #d4d4d4)); }
  .worker { border-left: 2px solid var(--vscode-focusBorder); padding: .2rem .5rem; margin: .4rem 0; }
  .worker.done, .worker.foreign { border-left-color: var(--vscode-panel-border); opacity: .55; }
  .who { font-weight: 600; }
  .doing { color: color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 70%, var(--vscode-editor-foreground, #d4d4d4)); font-size: .9em; }
  .reports { margin-left: .8rem; }
  .empty { color: color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 70%, var(--vscode-editor-foreground, #d4d4d4)); margin: 0; }
  .worker { cursor: pointer; }
  .worker:hover { background: var(--vscode-list-hoverBackground); }
</style></head>
<body><div class="rooms">${rooms}</div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
// Clicking a worker aims the side-bar Chat at them — what makes the workplace a control surface
// rather than a picture, ties the two panels together. Delegated, so it survives every
// re-render and works however the sprite is nested.
document.addEventListener("click", (event) => {
  const el = event.target && event.target.closest ? event.target.closest("[data-run-id]") : null;
  if (el) vscode.postMessage({ type: "focus", runId: el.getAttribute("data-run-id") });
});
</script>
</body></html>`;
}


/** The scene's markup ALONE — no document, no script — for updating a webview in place.
 *
 *  Replacing webview.html destroys the document: every sprite becomes a new element and every
 *  running animation dies, which is why a worker teleported between rooms instead of walking
 *  there. The panel builds the shell once and then posts this, so the engine inside can keep each
 *  body's position and finish a walk across an update.
 */
export function renderScene(
  state: TeamState,
  log?: { appendLine(line: string): void },
): string | null {
  if (scene === undefined) {
    try {
      scene = require("./workplace.js").renderScene ?? null;
    } catch (err) {
      log?.appendLine(`workplace scene unavailable: ${err}`);
      scene = null;
    }
  }
  if (!scene) return null;
  try {
    return scene(state);
  } catch (err) {
    log?.appendLine(`workplace scene failed to render: ${err}`);
    return null;
  }
}
