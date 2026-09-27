/** Writes the board out as plain pages so it can be opened in a browser and looked at.
 *
 *  A webview IS a browser, so the only thing missing outside VS Code is the theme: the
 *  --vscode-* variables the editor injects on the document element, and the class it puts on
 *  the body. Both are supplied here from the real Dark+ and Light+ values, injected exactly
 *  the way VS Code does it — so what renders in this harness is what renders in the panel.
 *  Same document, same CSP, same nonce, nothing stubbed.
 *
 *      node out/build.js <outdir>
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { renderBoard } from "./spindle";
import { STYLE } from "./style";
import { emptyBoard, fixture, quietBoard } from "./fixture";

/** Dark+ and Light+, read off the real editor. */
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

/** The same CSP the workplace ships under: no external anything, no image sources at all, one
 *  nonced script. Every pixel on the page is inline SVG or CSS, which is why it can hold. */
function page(bodyClass: string, vars: string, body: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src 'none'; font-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-proto';">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The spindle</title>
<style>:root{${vars}}</style>
<style>${STYLE}</style>
</head>
<body class="${bodyClass}">
${body}
</body>
</html>`;
}

/** The comparison sheet: every width and both themes on one page, so the direction can be
 *  judged in a single open without resizing anything. Theme is scoped to each column rather
 *  than to the body — the stylesheet asks for an ANCESTOR carrying the class, which the real
 *  editor's <body> satisfies just as well as a column here, so no rule is bent to make this
 *  work. */
function sheet(widths: number[]): string {
  const cols = ([["vscode-dark", DARK], ["vscode-light", LIGHT]] as const)
    .flatMap(([klass, vars]) =>
      widths.map(
        (w) =>
          `<div class="col"><div class="cap">${klass.replace("vscode-", "")} · ${w}px</div>` +
          // The font stack carries DOUBLE quotes ("Segoe WPC"), which close a style="..." attribute
          // early and silently drop everything after them — including the width, which is the one
          // thing this column exists to set. Single-quoted here; the real pages put the same vars
          // in a <style> block where quoting is not an issue.
          `<div class="${klass}" style="${vars.replace(/\n/g, "").replace(/"/g, "'")}width:${w}px;overflow:hidden">` +
          `${renderBoard(fixture())}</div></div>`,
      ),
    )
    .join("");
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src 'none'; font-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-proto';">
<title>The spindle — every width</title>
<style>${STYLE}</style>
<style>
  body { background:#2a2a2a; color:#ddd; font-family:system-ui,sans-serif; padding:16px; margin:0; }
  .row { display:flex; gap:18px; align-items:flex-start; }
  .cap { font-size:11px; letter-spacing:.14em; text-transform:uppercase; opacity:.6; margin-bottom:6px; }
  .col > div:last-child { border:1px solid #555; }
  .col .sp { min-height:0; }
  /* container-type:inline-size gives the panel size containment on the inline axis, so it
     contributes NO intrinsic width and a shrink-to-fit column folds it to nothing. Width must
     arrive from OUTSIDE: a fixed, non-shrinking column, with the panel filling it. Costs the
     real panel nothing — there it's a block in normal flow with a definite width already. */
  .col { flex: 0 0 auto; }
  .col .sp-panel { width: 100%; }
</style>
</head>
<body><div class="row">${cols}</div></body>
</html>`;
}

function main(): void {
  const out = process.argv[2] || ".";
  mkdirSync(out, { recursive: true });

  const pages: [string, string][] = [
    ["dark.html", page("vscode-dark", DARK, renderBoard(fixture()))],
    ["light.html", page("vscode-light", LIGHT, renderBoard(fixture()))],
    // The two boards that judge the BOTTOM of the panel rather than the top: a small team, which
    // is what most sessions actually look like, and none at all.
    ["quiet-dark.html", page("vscode-dark", DARK, renderBoard(quietBoard()))],
    ["quiet-light.html", page("vscode-light", LIGHT, renderBoard(quietBoard()))],
    ["empty.html", page("vscode-dark", DARK, renderBoard(emptyBoard()))],
    ["empty-light.html", page("vscode-light", LIGHT, renderBoard(emptyBoard()))],
    ["sheet.html", sheet([300, 220])],
  ];
  for (const [name, html] of pages) {
    writeFileSync(join(out, name), html, "utf8");
    console.log(`${name}  ${(html.length / 1024).toFixed(1)} kB`);
  }
}

main();
