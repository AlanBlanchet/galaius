/** The art bench: every composed TILE and a wall of PAPER DOLLS, on both themes.
 *
 *  The build-loop page for the Kenney substrate — the place to look when a tile mapping is
 *  wrong, a doll layer misaligns, or a hue picks an unreadable garment. Not the product; the
 *  product pages are `preview.ts`'s.
 *
 *      node out-sprites.js <outdir>
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { STYLE } from "../style";
import { TILES } from "../tiles";
import type { TileId } from "../tiles";
import { atlasDefs, dollOf, dollSvg, tileUse } from "../kenney";

const DARK = `
--vscode-editor-background:#1f1f1f;--vscode-editor-foreground:#cccccc;--vscode-foreground:#cccccc;
--vscode-descriptionForeground:#9d9d9d;--vscode-charts-blue:#4daafc;--vscode-charts-purple:#b180d7;
--vscode-charts-yellow:#d7ba7d;--vscode-charts-green:#89d185;--vscode-charts-orange:#d18616;
--vscode-charts-red:#f14c4c;--vscode-errorForeground:#f14c4c;`;
const LIGHT = `
--vscode-editor-background:#ffffff;--vscode-editor-foreground:#3b3b3b;--vscode-foreground:#3b3b3b;
--vscode-descriptionForeground:#767676;--vscode-charts-blue:#1a85ff;--vscode-charts-purple:#652d90;
--vscode-charts-yellow:#b89500;--vscode-charts-green:#388a34;--vscode-charts-orange:#d18616;
--vscode-charts-red:#e51400;--vscode-errorForeground:#e51400;`;

function tileCell(id: TileId, scale: number): string {
  const t = TILES[id];
  const minDy = Math.min(0, ...t.cells.map((c) => c.dy));
  const w = (Math.max(...t.cells.map((c) => c.dx)) + 1) * 16;
  const h = (0 - minDy + 1) * 16;
  return (
    `<figure class="sp ${id === "tree" || id === "pine" || id === "grass" ? "lawn" : "floor"}">` +
    `<svg viewBox="0 ${minDy * 16} ${w} ${h}" width="${w * scale}" height="${h * scale}">` +
    tileUse(id, 0, 0) +
    `</svg><figcaption>${id}</figcaption></figure>`
  );
}

function main(): void {
  const outDir = process.argv[2];
  if (!outDir) throw new Error("usage: sprites.js <outdir>");
  mkdirSync(outDir, { recursive: true });

  let body = "";

  body += `<h2>the dolls — seven pods, twelve runs each, scale 3</h2>`;
  for (let hue = 1; hue <= 7; hue++) {
    body += `<section>`;
    for (let i = 0; i < 12; i++) {
      const id = `run-${hue}-${i}`;
      body += `<figure class="sp floor"><span class="who">${dollSvg(dollOf(id, hue))}</span>` +
        `<figcaption>h${hue}</figcaption></figure>`;
    }
    body += `</section>`;
  }
  body += `<h2>foreign, and the brain</h2><section>`;
  body += `<figure class="sp floor"><span class="who">${dollSvg(dollOf("visitor-1", 1, { foreign: true }))}</span><figcaption>foreign</figcaption></figure>`;
  body += `<figure class="sp floor"><span class="who">${dollSvg(dollOf("the-brain", 3, { brain: true }))}</span><figcaption>brain</figcaption></figure>`;
  body += `</section>`;

  for (const scale of [2, 4]) {
    body += `<h2>tiles — scale ${scale}</h2><section>`;
    for (const id of Object.keys(TILES) as TileId[]) body += tileCell(id, scale);
    body += `</section>`;
  }

  const page = (vars: string, klass: string): string =>
    `<!DOCTYPE html><html><head><meta charset="utf-8"><style>:root{${vars}}</style>` +
    `<style>${STYLE}</style><style>
      body { margin: 0; }
      .wp-sheet { padding: 12px; display: block; height: auto; overflow: auto; }
      .wp-sheet h2 { color: var(--wp-fg); font: 700 12px sans-serif; text-transform: uppercase; margin: 18px 4px 6px; }
      .wp-sheet section { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; }
      .sp { margin: 0; padding: 8px; display: flex; flex-direction: column; gap: 4px; }
      .sp.floor { background: var(--t-floor); }
      .sp.lawn { background: var(--t-ground); }
      .sp figcaption { color: var(--wp-fg); font: 10px monospace; }
      .sp .who svg { width: 48px; height: 48px; }
      .sp svg { display: block; image-rendering: pixelated; }
    </style></head><body class="${klass}"><div class="wp">${atlasDefs()}` +
    `<div class="wp-sheet">${body}</div></div></body></html>`;

  writeFileSync(join(outDir, "sprites-dark.html"), page(DARK, "vscode-dark"));
  writeFileSync(join(outDir, "sprites-light.html"), page(LIGHT, "vscode-light"));
  console.log("sprites: 2 pages");
}

main();
