import { strict as assert } from "node:assert";
import { ROSTER_VIEWS, type RosterView } from "./railHtml.ts";
import { test } from "node:test";

import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { ZONES } from "./team.ts";

// Loaded from the COMPILED output rather than the source: `workplaceView.ts` imports `./team`
// without an extension (tsc emits, so extension-ful imports are not allowed), which node's
// type-stripping loader cannot resolve. Testing the built artifact is also closer to what ships.
const { plainRoom } = createRequire(import.meta.url)("../out/workplaceView.js");

// The plain room is the fallback when the pixel-art bundle is missing or throws. A code-reviewer
// run on this diff flagged that neither branch was covered, and that a bare catch would degrade
// silently for the life of a session with nothing saying why.

const STATE = {
  at: 1000,
  workers: [
    { run_id: "a", name: "reviewer", agent: "code-reviewer", status: "running" as const,
      zone: "code" as const, activity: "reading registry.py", parent_run_id: null,
      project: "interact", cost_usd: 1, input_tokens: 100, idle_seconds: 1 },
    { run_id: "b", name: "researcher", agent: "researcher", status: "running" as const,
      zone: "web" as const, activity: "searching the web", parent_run_id: "a",
      project: "interact", cost_usd: 0, input_tokens: 10, idle_seconds: 0 },
  ],
};

test("every zone gets a room, so nobody can be placed nowhere", () => {
  const html = plainRoom(STATE, "n1");
  for (const zone of ZONES) assert.ok(html.includes(zone.label), `no room for ${zone.label}`);
});

test("a worker stands in its own zone", () => {
  const html = plainRoom(STATE, "n1");
  const code = html.slice(html.indexOf("Code"), html.indexOf("Data"));
  assert.match(code, /reviewer/);
});

test("a sub-agent is drawn inside its parent, not loose in the room", () => {
  const html = plainRoom(STATE, "n1");
  const parent = html.indexOf("reviewer");
  assert.ok(html.indexOf("researcher") > parent, "the report follows the lead it belongs to");
});

test("every worker carries the run id the click-through needs", () => {
  const html = plainRoom(STATE, "n1");
  assert.match(html, /data-run-id="a"/);
  assert.match(html, /data-run-id="b"/);
});

test("an agent's name and activity are escaped — both are agent output", () => {
  const html = plainRoom(
    { at: 1, workers: [{ ...STATE.workers[0], name: "<img src=x>", activity: "</div><script>" }] },
    "n1",
  );
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes("<script>"));
});

test("an empty team renders a building, not a blank page", () => {
  const html = plainRoom({ at: 1, workers: [] }, "n1");
  assert.match(html, /Entry/);
  assert.match(html, /<html/);
});

// --- the document must stop being rebuilt ---
//
// Assigning `webview.html` destroys the document: every sprite becomes a new element and every
// running animation dies. That is WHY a worker teleported between rooms instead of walking there —
// the walk was a one-shot replay reconstructed from persisted state on the next load, not motion.
// The panel builds the shell once and then pushes scenes into it.

test("the scene can be rendered on its own, for pushing into a live document", () => {
  // Compiled output, for the same reason as plainRoom above.
  const { renderScene } = createRequire(import.meta.url)("../out/workplaceView.js");
  const html = renderScene({ workers: [], links: [], zones: [] });

  if (html === null) return; // no bundle in this checkout; the panel falls back to rebuilding
  assert.ok(!/<html|<body|<script/i.test(html), "a fragment, not a document");
});

test("the roster fills the panel at every width", () => {
  /* Superseded contract: the world used to be primary and the roster a toggle-open overlay under
     560px. His words — "remove the game like features and orient it more like we're doing in the
     web" — invert that, and with no room beside it the roster has no reason to hide or overlay. */
  const { renderWorkplace } = createRequire(import.meta.url)("../out/workplaceView.js");
  const html = renderWorkplace({ workers: [], links: [], zones: [] }, "n1", undefined, {
    style: "", body: "<p>Roster</p>", script: "",
  });
  assert.doesNotMatch(html, /\.wp-list \{ display: none/, "never hidden behind a toggle");
  assert.match(html, /<p>Roster<\/p>/);
});

test("a narrow panel gives the whole width to the roster", () => {
  /* Superseded: this asserted the 207px panel still left "usable map space" and contained the
     roster OVERLAY. There is no map and no overlay — the roster is the panel, so the only thing
     worth measuring is that it gets the width and does not scroll sideways. */
  const { renderWorkplace } = createRequire(import.meta.url)("../out/workplaceView.js");
  const html = renderWorkplace({ workers: [], links: [], zones: [] }, "n1", undefined, {
    style: "", body: "<p>Roster</p>", script: "",
  });
  assert.doesNotMatch(html, /position: absolute; z-index: 800/, "no overlay layer remains");
  assert.match(html, /\.wp-split > \.wp-list \{[^}]*flex: 1 1 auto/, "it takes the width");
});

test("a live workplace stops and restores motion when the VS Code body class changes", () => {
  const chrome = process.env.CHROME_BIN ?? "/usr/bin/google-chrome";
  assert.ok(fs.existsSync(chrome));
  const { renderWorkplace } = createRequire(import.meta.url)("../out/workplaceView.js");
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "out", "tests",
    "workplace-reduced-motion-contract");
  const page = path.join(root, "page.html");
  const profile = path.join(root, `profile-${process.pid}-${Date.now()}`);
  fs.mkdirSync(root, { recursive: true });
  const html = renderWorkplace(STATE, "n1").replace("</body>", `<script nonce="n1">
const animated=()=>[...document.querySelectorAll('*')]
  .filter((node)=>getComputedStyle(node).animationName!=='none').length;
const before=animated();
document.body.classList.add('vscode-reduce-motion');
setTimeout(()=>{
  const stopped=animated(); const still=window.__wp?.still===true;
  document.body.classList.remove('vscode-reduce-motion');
  setTimeout(()=>{
    const restored=animated(); const moving=window.__wp?.still===false;
    document.body.dataset.motionContract=before>0 && stopped===0 && still && restored>0 && moving
      ? 'ok-'+before+'-'+restored : 'failed-'+before+'-'+stopped+'-'+restored+'-'+still+'-'+moving;
  }, 25);
}, 25);
</script></body>`);
  fs.writeFileSync(page, html);
  const rendered = execFileSync(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu",
    `--user-data-dir=${profile}`, "--virtual-time-budget=1000", "--dump-dom", `file://${page}`],
  { encoding: "utf8" });
  assert.match(rendered, /data-motion-contract="ok-\d+-\d+"/,
    "the live body class must pause both CSS and the simulation, then restore normal motion");
});

test("the chosen roster view is remembered, and an unknown one is refused", () => {
  /* "All views so we can chose and store in configs (cache) so it reuses the same next time."
     The value round-trips through the same memento the agents tree uses for its grouping, and it
     arrives from a WEBVIEW — so it is validated against the known set rather than trusted and
     written straight into the class attribute of the roster. */
  const store = new Map<string, unknown>();
  const memento = {
    get: <T,>(k: string, d?: T) => (store.has(k) ? store.get(k) as T : d),
    update: async (k: string, v: unknown) => { store.set(k, v); },
  };
  const viewOf = (stored: unknown): RosterView => {
    store.set("interact.workplace.rosterView", stored);
    const got = memento.get<RosterView>("interact.workplace.rosterView");
    return ROSTER_VIEWS.some((v) => v.id === got) ? got as RosterView : "grouped";
  };
  assert.equal(viewOf("table"), "table", "a stored choice comes back");
  assert.equal(viewOf("cards"), "grouped", "a view that no longer exists falls back, never renders");
  assert.equal(viewOf("<script>"), "grouped", "an unknown value falls back, never renders");
  assert.equal(viewOf(undefined), "grouped", "and a first run has a default");
});

test("the Team tab opens on the roster, and the world simulation is not mounted", () => {
  /* "the Interact - Team is just super laggy. Maybe we should remove the game like features and
     orient it more like we're doing in the web."

     The lag is not subtle and it is not the roster: the tile world is ~9,700 lines across
     world/sim/scene/tiles, it drove the renderer to 149% CPU and 2.99 GB RSS, and the project's
     own `test_forty_eight_actors_hold_frame_budget` fails at 23.7ms against a 20ms budget. A
     surface you cannot type into is not a surface. The roster is what the tab shows now; the
     scene is not rendered, so the simulation never starts. */
  const { renderWorkplace } = createRequire(import.meta.url)("../out/workplaceView.js");
  const html = renderWorkplace({ workers: [], links: [], zones: [] }, "n1", undefined, {
    style: "", body: "<div id='roster'>rows</div>", script: "",
  });
  assert.doesNotMatch(html, /class="wp-room"/, "no room element to mount a scene into");
  assert.doesNotMatch(html, /wp-roster-toggle/, "the roster is not a thing you toggle open");
  assert.match(html, /id='roster'/, "the roster IS the surface");
});

test("the roster keeps the container its repaint handler looks for", () => {
  /* Round 46 FAIL, and self-inflicted: `.wp-list` was the roster's container in the old split.
     Collapsing the layout deleted it, and `railScript` opens every repaint with
     `querySelector(".wp-list"); if (!host) return;` — so pushRoster() was dropped on the floor.
     Not chooser-specific: the roster never repainted at all, so live agent updates and drill-in
     died with it. Same class as the `.rail`/`.runs` bug: a selector naming a deleted element. */
  const { renderWorkplace } = createRequire(import.meta.url)("../out/workplaceView.js");
  const html = renderWorkplace({ workers: [], links: [], zones: [] }, "n1", undefined, {
    style: "", body: "<p>Roster</p>", script: "",
  });
  assert.match(html, /class="wp-list"/, "the repaint host must exist");
  const { railScript } = createRequire(import.meta.url)("../out/railHtml.js");
  const host = railScript().match(/querySelector\("([^"]+)"\)[^;]*;\s*if \(!host\)/);
  assert.ok(host, "the script still resolves a host");
  assert.match(html, new RegExp(`class="${host[1].replace(".", "")}"`),
    `the script looks for ${host[1]} and the document must contain it`);
});

test("the roster gets the whole panel — one rule, not two fighting", () => {
  /* Round 47, BLOCKING: two `.wp-split > .wp-list` rules at equal specificity, the later one
     winning with `flex: 0 0 clamp(240px, 26%, 380px)` — the old sidebar width from when a world
     sat beside it. Live full-width Team tab: 367px of roster against 1045px of empty, 1540px at
     1920. It also made the 560px container query permanently true, so the table could never
     render as a table. Same shape as the duplicate `min-width` that silently deleted a floor. */
  const { renderWorkplace } = createRequire(import.meta.url)("../out/workplaceView.js");
  const html = renderWorkplace({ workers: [], links: [], zones: [] }, "n1", undefined, {
    style: "", body: "<p>Roster</p>", script: "",
  });
  // The invariant is about WIDTH, not about the selector text: `railHtml` legitimately raises
  // specificity on the same element to restore the board background, and sets no width at all.
  const widths = [...html.matchAll(/\.wp-split > (?:div\.)?\.?wp-list \{[^}]*?(flex:[^;]+;)/g)]
    .map((m) => m[1]);
  assert.equal(widths.length, 1, `exactly one rule may set the width, found ${widths.length}: ${widths}`);
  assert.match(widths[0], /flex: 1 1 auto/, "and it gives the roster the panel");
  assert.doesNotMatch(html, /clamp\(240px/, "the old sidebar cap is gone");
});
