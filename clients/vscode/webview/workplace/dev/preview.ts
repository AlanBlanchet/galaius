/** Writes the view out as plain pages so it can be opened in a browser and looked at.
 *
 *  A webview is a browser, so the only thing missing outside VS Code is the theme: the
 *  --vscode-* variables the editor injects. Those are supplied here from the real Dark+ and
 *  Light+ values, so what renders in this harness is what renders in the panel — the same
 *  document, same CSP, same nonce, nothing stubbed.
 *
 *      node out-preview.js <outdir>
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { renderActors, renderWorkplace } from "../index";
import { fixture, fixtureMoved } from "./fixture";

const DARK = `
--vscode-editor-background:#1f1f1f;--vscode-editor-foreground:#cccccc;--vscode-foreground:#cccccc;
--vscode-descriptionForeground:#9d9d9d;--vscode-widget-border:#3c3c3c;--vscode-focusBorder:#0078d4;
--vscode-charts-blue:#4daafc;--vscode-charts-purple:#b180d7;--vscode-charts-yellow:#d7ba7d;
--vscode-charts-green:#89d185;--vscode-charts-orange:#d18616;--vscode-charts-red:#f14c4c;
--vscode-errorForeground:#f14c4c;
--vscode-font-family:"Segoe WPC","Segoe UI",system-ui,-apple-system,sans-serif;`;

const LIGHT = `
--vscode-editor-background:#ffffff;--vscode-editor-foreground:#3b3b3b;--vscode-foreground:#3b3b3b;
--vscode-descriptionForeground:#767676;--vscode-widget-border:#d4d4d4;--vscode-focusBorder:#0090f1;
--vscode-charts-blue:#1a85ff;--vscode-charts-purple:#652d90;--vscode-charts-yellow:#b89500;
--vscode-charts-green:#388a34;--vscode-charts-orange:#d18616;--vscode-charts-red:#e51400;
--vscode-errorForeground:#e51400;
--vscode-font-family:"Segoe WPC","Segoe UI",system-ui,-apple-system,sans-serif;`;

/** The editor sets the theme variables on the document element and a class on the body. Injected
 *  the same way here, after the view's own stylesheet, exactly as VS Code does. */
function themed(html: string, vars: string, klass: string): string {
  return html
    .replace("</head>", `<style>:root{${vars}}</style></head>`)
    .replace("<body>", `<body class="${klass}">`);
}

/** A page that DRIVES the engine, which is the only way motion can be looked at or measured.
 *
 *  A still says nothing about movement and a pair of stills says nothing either — exactly how a
 *  teleport passes review. So the harness ships the host's own protocol: the shell is rendered
 *  once, and a driver posts successive scenes into it the way the panel will, cycling between
 *  two snapshots of the same team so people are continually being sent somewhere.
 *
 *  window.__wpPhase(i) jumps straight to a snapshot, so a probe can step the whole thing rather
 *  than wait for it.
 */
function live(base: string, vars: string, klass: string, every = 7000): string {
  const scenes = [renderActors(fixture()), renderActors(fixtureMoved())];
  const driver =
    `<script nonce="devnonce123">` +
    `var WP_SCENES=${JSON.stringify(scenes)};var wpAt=0;` +
    `window.__wpPhase=function(i){wpAt=i%WP_SCENES.length;window.__wpApply(WP_SCENES[wpAt]);};` +
    `window.__wpNext=function(){window.__wpPhase(wpAt+1);};` +
    `setTimeout(function(){setInterval(window.__wpNext,${every});},${every});` +
    `</script>`;
  return themed(base, vars, klass).replace("</body>", `${driver}</body>`);
}

/** The whole of one element, balanced. The map nests an svg per floor pattern and the sprite
 *  sheet nests one per tile, so the first </svg> after either opening tag is somebody else's. */
function element(doc: string, start: number, tag: string): string {
  const re = new RegExp(`<${tag}\\b|</${tag}>`, "g");
  re.lastIndex = start;
  let depth = 0;
  for (let m = re.exec(doc); m; m = re.exec(doc)) {
    depth += m[0][1] === "/" ? -1 : 1;
    if (depth === 0) return doc.slice(start, m.index + tag.length + 3);
  }
  throw new Error(`unbalanced <${tag}> at ${start}`);
}

/** The map and the sheet it draws from, on a page with no viewport, no camera and no motion.
 *
 *  The product's own pages can't answer "is this thing's shade in the right place": the camera
 *  is over somewhere else, the zoom is a variable, and ambient animation moves the canopies
 *  between two captures so a difference image comes back contaminated. Here the map sits at its
 *  natural size at the origin, so tile (x, y) is at (24x, 24y) on every screenshot and two
 *  builds crop identically. Same document, same stylesheet, same theme variables — only the
 *  window is gone. */
function flat(vars: string, klass: string): string {
  const doc = renderWorkplace(fixture(), "devnonce123");
  const style = doc.slice(doc.indexOf("<style"), doc.indexOf("</style>") + 8);
  const sheet = element(doc, doc.indexOf('<svg class="wp-defs"'), "svg");
  const map = element(doc, doc.indexOf('<svg class="wp-map"'), "svg");
  return (
    `<!DOCTYPE html><html><head><meta charset="utf-8"><style>:root{${vars}}</style>${style}` +
    `<style>html,body{margin:0;padding:0;background:var(--vscode-editor-background)}` +
    `.wp{position:static;width:auto;height:auto;overflow:visible}svg.wp-map{display:block}` +
    `*{animation:none !important;transition:none !important}</style></head>` +
    `<body class="${klass}"><div class="wp" data-zoom="1">${sheet}${map}</div></body></html>`
  );
}

function main(): void {
  const out = process.argv[2] || ".";
  mkdirSync(out, { recursive: true });

  const pages: [string, string][] = [
    ["dark.html", themed(renderWorkplace(fixture(), "devnonce123"), DARK, "vscode-dark")],
    ["light.html", themed(renderWorkplace(fixture(), "devnonce123"), LIGHT, "vscode-light")],
    ["move-a.html", themed(renderWorkplace(fixture(), "devnonce123"), DARK, "vscode-dark")],
    ["move-b.html", themed(renderWorkplace(fixtureMoved(), "devnonce123"), DARK, "vscode-dark")],
    // A FIXED stamp, not Date.now(): a harness page that bakes the wall clock cannot be diffed
    // against itself, so it reports a change on every rebuild and hides a real one.
    ["empty.html", themed(
      renderWorkplace({ workers: [], at: Date.UTC(2026, 7, 18, 14, 3, 22) }, "devnonce123"),
      DARK, "vscode-dark",
    )],
    // The pages that MOVE. Everything about this turn is judged on these, never on the stills.
    ["live.html", live(renderWorkplace(fixture(), "devnonce123"), DARK, "vscode-dark")],
    // The same team with the snapshot stamped the way the DATA LAYER stamps it — epoch SECONDS,
    // not milliseconds. Every other page here happens to use Date.UTC, which is milliseconds,
    // so the harness showed a correct clock while the panel showed 1970. A fixture that only
    // ever exercises the convenient unit is a fixture that certifies the bug.
    ["prod-units.html", themed(
      renderWorkplace(fixture(Date.UTC(2026, 7, 18, 14, 3, 22) / 1000), "devnonce123"),
      DARK, "vscode-dark",
    )],
    ["live-light.html", live(renderWorkplace(fixture(), "devnonce123"), LIGHT, "vscode-light")],
    // THE WHOLE MAP AT 1:1, WITH NO CAMERA ON IT. Every other page here is the product — a
    // viewport somewhere over a building deliberately bigger than it, fine for judging the
    // product and useless for judging the GROUND, since what you want to look at is usually off
    // screen and the two things you want to compare are never in frame at once. Shadows were
    // checked three times against pages like that. This one is the map alone, so a crop is a
    // fixed tile rectangle and a before/after is two files.
    ["flat-dark.html", flat(DARK, "vscode-dark")],
    ["flat-light.html", flat(LIGHT, "vscode-light")],
  ];
  for (const [name, html] of pages) {
    writeFileSync(join(out, name), html, "utf8");
    console.log(`${name}  ${(html.length / 1024).toFixed(1)} kB`);
  }
}

main();
