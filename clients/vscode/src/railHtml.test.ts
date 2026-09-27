/** The rail as it actually renders.
 *
 *  The whole point of moving off a `TreeView` is that chrome can be VISIBLE AT REST, so these
 *  tests are mostly about what exists in the document without anyone hovering, focusing, or
 *  selecting an agent first. Each one corresponds to a measured defect in the surface it replaces.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { buildRail } from "./rail.ts";
import { railHtml, railBody, railStyle, headerLine, railScript, modelsOnScreen,
  ROSTER_VIEWS, type RosterView } from "./railHtml.ts";
import { voiceOf } from "./statusLanguage.ts";
import { conversationTitle, roleOf } from "./roster.ts";
import { actionsFor } from "./agentActions.ts";

const run = (over: Record<string, unknown> = {}) => ({
  run_id: "r1", name: "visual-critic", provider: "claude", status: "running", started_at: 100,
  ...over,
}) as never;

const html = (runs: unknown[] = [run()], scope = "interact") =>
  railHtml(buildRail(runs as never[], scope, () => 0), "N0NCE", voiceOf,
    (r) => conversationTitle(r as never), (r) => ({ id: roleOf(r as never).id, label: roleOf(r as never).id }), (r) => actionsFor(r as never));

test("every destination is in the document at rest, with its word", () => {
  // The defect this replaces: VS Code renders a view's title actions only while the pointer is in
  // the header, and clips the overflow with no "…" menu. At rest the panel had NO buttons.
  const doc = html();
  // Two destinations now, not four: the dashboard and the sequence view are their own surfaces and
  // live in the palette. What a panel this narrow must still offer at rest is the world and a way
  // to start someone — see CHIPS.
  for (const label of ["Team", "+ Session", "Staff"]) {
    assert.ok(doc.includes(`>${label}<`), `"${label}" is not present without hovering`);
  }
  assert.match(doc, /data-command="interact\.openDashboard"[^>]*>⚙ Settings</,
    "the Team gear must open the same Interact settings workspace as Conversation");
});

test("the scope is stated on the surface, not hidden in a dialog", () => {
  assert.ok(html([run()], "sheets").includes("sheets"),
    "whose agents these are must be readable without opening anything");
});

test("an empty team still renders every destination", () => {
  // The old empty state offered nothing at all: no agents meant no composer, so no way to reach
  // the team either. Empty is exactly when you most need the buttons.
  const doc = html([]);
  assert.ok(doc.includes(">Team<"));
  assert.ok(doc.includes("nobody working yet"));
});

test("a run that needs you is marked, not merely listed", () => {
  const doc = html([run({ run_id: "bad", name: "perf-critic", status: "crashed" })]);
  assert.ok(doc.includes("perf-critic"));
  /* The prose note ("stopped with an error") left with the one-row grammar — the WORD carries the
     state now, exactly as the workplace stamps it, and the row spends its width on what/when/cost. */
  assert.ok(doc.includes(">ERROR<"));
  assert.match(doc, /data-attention="error"/);
});

test("agent output is escaped — it is untrusted text in a webview", () => {
  const doc = html([run({ name: "<img src=x onerror=alert(1)>" })]);
  assert.ok(!doc.includes("<img src=x"), "an agent's name reached the DOM as markup");
  assert.ok(doc.includes("&lt;img"));
});

test("the script carries the nonce the CSP demands", () => {
  const doc = html();
  assert.match(doc, /<script nonce="N0NCE"/);
  assert.match(doc, /Content-Security-Policy/);
  // A script without the nonce is silently dropped, which is how a whole panel's behaviour
  // vanished before with nothing on screen to say so.
  assert.ok(!/<script(?![^>]*nonce)/.test(doc), "a script would be blocked by the CSP");
});

test("it themes off body, not :root", () => {
  // VS Code injects its --vscode-* variables onto <body>. A :root block never resolves and
  // silently falls back — the light theme rendered as unstyled once for exactly this reason.
  const doc = html();
  assert.ok(!/:root\s*\{[^}]*--vscode-/.test(doc),
    "a :root block will not see the host's theme variables");
});

test("each row can be actuated, and says which run it is", () => {
  const doc = html([run({ run_id: "abc123" })]);
  assert.match(doc, /data-run="abc123"/);
});

test("an empty team reads as a sentence, not as zeroes", () => {
  assert.equal(headerLine(buildRail([], "x", () => 0).header), "nobody working yet");
});

test("the header line leads with what needs you", () => {
  const built = buildRail([run({ run_id: "a" }), run({ run_id: "b", status: "failed" })] as never[], "x", () => 0);
  assert.match(headerLine(built.header), /^1 needs you/);
});

test("dim text never uses the raw description token", () => {
  // VS Code ships --vscode-descriptionForeground at 4.28:1 in its own Light theme — under AA
  // before any panel touches it. themeTokens.ts exists for exactly this, and a second copy of the
  // workaround is how the first one ended up covering two of seven sites.
  const doc = html();
  assert.ok(!/color:\s*var\(--vscode-descriptionForeground\)/.test(doc),
    "raw descriptionForeground is sub-AA in light; pull it toward the foreground");
});

test("the orchestrator is marked on the surface, not just in the data", () => {
  const doc = html([
    run({ run_id: "boss", name: "main", started_at: 100 }),
    run({ run_id: "kid", name: "tester", parent_run_id: "boss", started_at: 200 }),
  ]);
  assert.match(doc, /data-brain="1"/, "nothing tells you which one you steer from");
  assert.ok(doc.includes(">brain<"));
});

test("a report is marked as one, so the roster reads as a company", () => {
  const doc = html([
    run({ run_id: "boss", name: "main", started_at: 100 }),
    run({ run_id: "kid", name: "tester", parent_run_id: "boss", started_at: 200 }),
  ]);
  assert.match(doc, /data-report="1"/, "a sub-agent floats loose beside its lead");
});

test("each row carries the actions that would actually work on it", () => {
  // "They should have actions for these. And we should show they actions in a ergonomic way."
  // Built, tested, and consumed by nothing until now — the orphan pattern that has landed eight
  // times in this repo.
  const doc = html([run({ run_id: "live", name: "artist", status: "running" })]);
  assert.match(doc, /data-action="stop"/, "a running agent can be stopped");
  assert.match(doc, /data-action="message"/);
  assert.match(doc, /data-action="transcript"/);
});

test("a finished agent offers no stop, so no control on screen is dead", () => {
  const doc = html([run({ run_id: "over", name: "librarian", status: "done" })]);
  assert.ok(!doc.includes('data-action="stop"'));
  assert.match(doc, /data-action="transcript"/, "its transcript is always readable");
});

test("every action carries a title, since a bare glyph is a guess", () => {
  const doc = html([run({ run_id: "live", status: "running" })]);
  for (const m of doc.matchAll(/<button class="act"[^>]*>/g)) {
    assert.match(m[0], /title="/, `an action button with no title: ${m[0]}`);
  }
});

test("the rail and the world use the same status vocabulary", () => {
  /* The rail now has enough width to say every status at rest. The word still comes from the
     shared vocabulary, so the wider metadata grid does not create a second state language. */
  const doneRow = html([run({ status: "completed" })]);
  assert.ok(doneRow.includes(`>${voiceOf("finished").word}`), "a finished row says its state");
  assert.ok(doneRow.includes(voiceOf("finished").mark), "the mark reinforces the state");

  const errRow = html([run({ status: "failed" })]);
  const err = voiceOf("error");
  assert.ok(errRow.includes(`>${err.word}<`), "an error must still SAY so, in the shared word");
});


test("each state carries its own accent, so colour still separates them", () => {
  const finished = html([run({ status: "completed" })]);
  const failed = html([run({ status: "failed" })]);
  assert.ok(finished.includes(`var(${voiceOf("finished").accent})`));
  assert.ok(failed.includes(`var(${voiceOf("error").accent})`));
  assert.notEqual(voiceOf("finished").accent, voiceOf("error").accent);
});

/* ——— Rest density: outcomes at rest, machinery behind intent ——— */

test("the wide rail shows the row's facts and actions at rest", () => {
  const css = railStyle();
  assert.match(css, /\.runs \.row, \.columnHead\s*{[^}]*display:\s*grid/, "wide rows use the full rail");
  assert.match(css, /\.acts\s*{[^}]*opacity:\s*1/, "actions are visible at rest");
  assert.match(css, /\.actText/, "actions explain themselves without hover");
  assert.match(css, /@container \(max-width: 1380px\)/, "narrow rails have a responsive fallback");
});

test("a row surfaces status, provider, model and effort only from its run", () => {
  const doc = railHtml(
    buildRail([run({ provider: "codex", model: "gpt-5.6-luna", reasoning: "high" })] as never[], "interact", () => 0),
    "N0NCE", voiceOf, (r) => conversationTitle(r as never),
    (r) => ({ id: roleOf(r as never).id, label: roleOf(r as never).id }),
    () => actionsFor({ run_id: "r1", status: "running" }),
    () => ({ text: "90 · 1st", title: "gpt-5.6-luna — aa.intelligence 90 · 1st" }),
    (r) => (r as { model?: string }).model,
  );
  assert.match(doc, /class="stamp"[^>]*>.*WORKING/, "status word is readable, not only a dot");
  assert.match(doc, /class="columnHead"[^>]*>.*<span>Status<\/span>/,
    "metadata columns say what each value means");
  assert.match(doc, /class="provider"[^>]*>codex<\/span>/);
  assert.match(doc, /class="model"[^>]*>gpt-5\.6-luna<\/span>/);
  assert.match(doc, /class="effort"[^>]*>high<\/span>/);
  assert.match(doc, /class="score"[^>]*>90 · 1st<\/span>/);
  assert.match(doc, /data-action="message"[^>]*aria-label="Say something to them"/);
});

test("the ledger is one line that opens, not seven rows that shout", () => {
  const built = { header: { scope: "interact", working: 0, needsYou: 0, finished: 0 },
    chips: [], staff: [],
    ledger: { runs: [
      { run: { run_id: "o1", name: "t", provider: "claude", status: "done", started_at: 100, cost_usd: 2 }, attention: "finished", brain: false, depth: 0, note: "finished" },
      { run: { run_id: "o2", name: "t", provider: "claude", status: "stopped", started_at: 90 }, attention: "stopped", brain: false, depth: 0, note: "stopped" },
    ], done: 1, stopped: 1, failed: 0, cost: 2 },
    runs: [] } as never;
  const html = railBody(built, voiceOf as never, (r: { name: string }) => r.name as never,
    (() => ({ id: "t", label: "t" })) as never);
  assert.match(html, /<details class="ledger">/);
  assert.match(html, /1 done · 1 stopped · \$2\.00/);
  assert.ok(html.indexOf("</details>") > html.indexOf('data-run="o1"'), "old rows live inside it");
});

test("staff rest below, named and quiet", () => {
  const built = { header: { scope: "interact", working: 0, needsYou: 0, finished: 0 },
    chips: [], ledger: null,
    staff: [{ run: { run_id: "decl:critic", name: "Visual QA authority", provider: "claude", status: "declared" }, attention: "ready", brain: false, depth: 0, note: "ready" }],
    runs: [{ run: { run_id: "live", name: "live", provider: "claude", status: "running", started_at: 100 }, attention: "working", brain: false, depth: 0, note: "working" }] } as never;
  const html = railBody(built, voiceOf as never, (r: { name: string }) => r.name as never,
    (() => ({ id: "x", label: "x" })) as never);
  assert.match(html, /class="staffHead"/);
  assert.ok(html.indexOf('data-run="decl:critic"') > html.indexOf('data-run="live"'),
    "the resting staff never sit above the working team");
});

test("a one-line row: the title never wraps into a paragraph", () => {
  assert.match(railStyle(), /\.who\s*{[^}]*text-overflow:\s*ellipsis/,
    "a task sentence is clipped, not a wall");
});


test("a roster repaint keeps what the reader had opened, typed and scrolled", () => {
  /* The Team tab's ON STAFF section folded on every repaint — a model picked, a run finishing —
     because the roster is swapped wholesale by innerHTML and <details> state lives only in the
     DOM. The swap must snapshot and reapply it; this pins the script to that shape. */
  const script = railScript();
  const swap = script.indexOf("host.innerHTML = html");
  assert.ok(swap > 0, "the roster swap must exist");
  const before = script.slice(0, swap), after = script.slice(swap);
  assert.match(before, /open\[d\.dataset\.foldKey \|\| d\.className\] = d\.open/, "snapshot each task or section independently BEFORE the swap");
  assert.match(after, /d\.open = open\[key\]/, "reapply it AFTER the swap");
  assert.match(after, /host\.scrollTop = top/, "and the scroll position");
});

test("clearing the filter gives the reader back the folds THEY had open", () => {
  /* Searching opens every fold so a match cannot hide; clearing used to re-fold everything to a
     "resting state" — which folded an expanded ON STAFF shut on every clear. The script must
     remember the reader's own state when a search starts and restore it when it ends. */
  const script = railScript();
  assert.doesNotMatch(script, /d\.open = Boolean\(q\)/, "the blanket re-fold must be gone");
  assert.match(script, /folded = details\.map\(\(d\) => d\.open\)/, "remember the folds when a search starts");
  assert.match(script, /d\.open = fold\.folded\[i\]/, "restore them when it ends");
});

test("the remembered folds survive a repaint during the search", () => {
  /* A model picked while a filter was on repainted the roster; the fold snapshot lived in the
     closure that repaint re-bound, so the search's forced-open state was "restored" on clear —
     History left open, ON STAFF pushed below the fold. The snapshot lives on the window. */
  const script = railScript();
  assert.match(script, /window\.__railFold = window\.__railFold \|\| \{ folded: null \}/);
  assert.match(script, /d\.open = fold\.folded\[i\]/, "restore from the window-held snapshot");
});

test("a repaint waits while the reader's keyboard is on a row", () => {
  /* Twelve trusted Tabs never reached a row. Each Tab stepped into the list, the ~1/s wholesale
     swap destroyed the element under it, focus fell back to <body>, and the next Tab started over
     from the first control — a loop. Putting focus back onto a row rescues focus already ON one;
     it cannot rescue focus trying to ENTER. The repaint has to wait instead. */
  const script = railScript();
  assert.match(script, /window\.__railPending = m\.html/, "the newest roster is held, never dropped");
  assert.match(script, /host\.contains\(act\)/, "deferred while focus is on a row inside the list");
  assert.match(script, /"focusout"/, "and applied when the keyboard leaves");
  assert.match(script, /host\.contains\(ev\.relatedTarget\)/, "row-to-row movement keeps waiting");
  assert.match(script, /window\.addEventListener\("blur", flush\)/,
    "and a backgrounded window catches up rather than freezing on a parked row");
  // Three ways this could wedge, each closed: armed while the window was ALREADY blurred (blur
  // would never fire again), a held roster applied into a node a re-render detached, and no
  // backstop at all if neither event ever comes.
  assert.match(script, /document\.hasFocus\(\)/, "never defer for a reader who is not there");
  assert.match(script, /document\.querySelector\("\.wp-list"\) \|\| host/,
    "flush re-queries the host rather than using the one it captured");
  assert.match(script, /setTimeout\(flush, 30000\)/, "and a cap releases it regardless");
});

test("a roster row says WHICH kind of nothing it has, and never a bare blank", () => {
  /* A visual critic caught these cells three separate rounds and no test held them. Three
     different facts were being told with one sentence, or with none at all:
       - a colleague with a measure shows it;
       - a colleague whose model is simply not on the board says so;
       - a colleague with NO model recorded inherits, and the vendor picks at spawn — printing
         "no ranking places inherit" told 22 rows the wrong thing about a sentinel.
     `railBody` is pure, so all three are checkable here rather than in a screenshot. */
  const rail = buildRail(
    [
      { run_id: "a1", name: "tester", agent: "tester", status: "done", started_at: 1,
        model: "claude-sonnet-5" },
      { run_id: "b2", name: "scraper", agent: "scraper", status: "done", started_at: 2,
        model: "some-unranked-model" },
      { run_id: "c3", name: "advocate", agent: "advocate", status: "done", started_at: 3 },
    ] as never[],
    "", () => 0, undefined, undefined,
    (r) => ({ id: (r as { agent?: string }).agent ?? "main", label: (r as { name: string }).name }),
    Date.now(),
  );
  const html = railBody(
    rail, voiceOf, (r) => conversationTitle(r as never),
    (r) => ({ id: (r as { agent?: string }).agent ?? "main", label: (r as { name: string }).name }),
    () => [],
    (r) => ((r as { model?: string }).model === "claude-sonnet-5"
      ? { text: "38.4 · 24th", title: "claude-sonnet-5 — aa.intelligence 38.4 · 24th of 450 scored" }
      : undefined),
    (r) => (r as { model?: string }).model,
  );

  assert.match(html, /38\.4 · 24th/, "a measured colleague shows its number");
  assert.match(html, /aa\.intelligence 38\.4 · 24th of 450 scored/, "and the full sentence titles it");
  // The two absences are DIFFERENT and must not share a sentence.
  assert.doesNotMatch(html, /no ranking places inherit/,
    "`inherit` is the sentinel for nothing-recorded, never a model name");
  // The legend appears only once something carries a measure — it names the source, which
  // otherwise exists nowhere at rest.
  assert.match(html, /Artificial Analysis intelligence · one measure, not a verdict/);
});

test("every bucket that renders a row is one the roster asks about", () => {
  /* `railBody` draws rows from FOUR collections — the live runs, your own sessions, the history
     ledger and the staff fold — and two separate things only looked at the first. The legend
     vanished exactly when rows had numbers; and the panel asked the ranking about one bucket's
     models, so a colleague sitting in "on staff" was rendered with `no ranking places sonnet`
     beside five `claude-sonnet-5` rows showing `38.4 · 24th`. One model, two answers, one screen.

     Fixing the legend without fixing the ask was solving the instance instead of the class, so
     the enumeration now lives in ONE place and both callers use it. */
  const rail = {
    runs: [{ run: { run_id: "a", model: "claude-sonnet-5" } }],
    yours: [{ run: { run_id: "b", model: "gpt-6-astra" } }],
    ledger: { done: 1, stopped: 0, failed: 0, cost: 0,
              runs: [{ run: { run_id: "c", model: "claude-opus-5" } }] },
    staff: [{ run: { run_id: "d", model: "sonnet" } }],
  } as never;
  assert.deepEqual(modelsOnScreen(rail).sort(),
    ["claude-opus-5", "claude-sonnet-5", "gpt-6-astra", "sonnet"],
    "a model is asked about wherever it is drawn, not only in the first collection");

  // Absent buckets are ordinary, and a run with no model recorded is not a model.
  assert.deepEqual(modelsOnScreen({ runs: [{ run: { run_id: "e" } }] } as never), []);
});

test("the roster renders in whichever of the three views you chose", () => {
  /* "All views so we can chose and store in configs (cache) so it reuses the same next time."
     One row model, three layouts: the view is a property of the CONTAINER, not of the row, so a
     table, a card grid and today's grouped list are three CSS regimes over identical markup
     rather than three renderers to keep in step. */
  const built = { header: { scope: "interact", working: 0, needsYou: 0, finished: 0 },
    chips: [], ledger: null,
    runs: [{ run: { run_id: "r1", name: "tester", provider: "claude", status: "running",
                    started_at: 100 },
             attention: "working", brain: false, depth: 0, note: "working" }] } as never;
  const of = (view: RosterView) => railBody(built, voiceOf as never,
    (r: { name: string }) => r.name as never, (r: { name: string }) => ({ id: r.name, label: r.name }) as never,
    () => [], () => undefined, () => undefined, view);

  for (const view of ROSTER_VIEWS) {
    const html = of(view.id);
    assert.match(html, new RegExp(`class="roster view-${view.id}"`), `${view.id} names itself on the container`);
    assert.match(html, new RegExp(`data-view="${view.id}"[^>]*aria-pressed="true"`),
      `${view.id} reads as the current choice in the chooser`);
    // Every view offers every other view, or a choice made once cannot be unmade.
    for (const other of ROSTER_VIEWS) assert.match(html, new RegExp(`data-view="${other.id}"`));
  }
  // The row markup itself is identical across views — that is what makes them cheap.
  const rowOf = (html: string) => html.slice(html.indexOf('<li class="row"'), html.indexOf("</li>"));
  assert.equal(rowOf(of("table")), rowOf(of("grouped")));
});

test("each roster view brings a layout, not just a name", () => {
  /* A chooser that changes a class and nothing else is a chooser that does nothing. */
  const style = railStyle();
  assert.match(style, /\.view-table [^{]*\.row \{[^}]*display: *grid/, "table aligns its columns");
  assert.doesNotMatch(style, /view-cards/, "cards was removed for costing 1.735x the height");
});

test("the view class sits on an ancestor of the rows it restyles", () => {
  /* VERDICT FAIL, round 45: `.rail` and `.runs` are SIBLINGS, so every `.view-table .runs .row`
     rule could never match — the chooser set a class that styled nothing. The unit test that
     passed asserted the class was PRESENT; presence is not containment. */
  const built = { header: { scope: "interact", working: 0, needsYou: 0, finished: 0 },
    chips: [], ledger: null,
    runs: [{ run: { run_id: "r1", name: "tester", provider: "claude", status: "running",
                    started_at: 100 },
             attention: "working", brain: false, depth: 0, note: "working" }] } as never;
  const html = railBody(built, voiceOf as never, (r: { name: string }) => r.name as never,
    (r: { name: string }) => ({ id: r.name, label: r.name }) as never,
    () => [], () => undefined, () => undefined, "table");
  const open = html.indexOf('class="roster view-table"');
  assert.ok(open >= 0, "a container carries the view");
  assert.ok(open < html.indexOf('<ul class="runs"'), "and it OPENS before the rows it restyles");
  assert.ok(html.trimEnd().endsWith("</div>"), "and closes around them");
});

test("table columns are fixed, so they line up down the whole list", () => {
  /* Forcing the class on proved the rules otherwise sound but the columns still did not align:
     `auto` tracks size per ROW, so each row picked its own widths — 8 distinct score-cell x
     positions spread over 267px. Fixed trailing tracks make every row compute identically. */
  const style = railStyle();
  const rule = style.slice(style.indexOf(".view-table .runs .row"));
  const cols = rule.slice(rule.indexOf("grid-template-columns"), rule.indexOf(";", rule.indexOf("grid-template-columns")));
  assert.ok(!/\bauto\b/.test(cols), `trailing tracks must not be auto-sized: ${cols}`);
  assert.match(style, /\.runs \.who \{[^}]*min-width/, "the name keeps a floor, never 0px");
});

test("a repaint waits for a reader on a ROW, never for the control that asked for it", () => {
  /* Round 48, blocking, 30.2 s measured flip latency. The deferral exists so a live repaint does
     not yank focus out of the list while someone is tabbing through ROWS. But it tested "focus is
     anywhere inside .wp-list" — and the view chooser lives inside .wp-list, so clicking a view
     focused the button, which deferred the very repaint that click had just requested. It read as
     intermittent only because any unrelated click flushed the queue.

     The guard belongs on what it was protecting: a focused ROW. */
  const script = railScript();
  assert.doesNotMatch(script, /host\.contains\(document\.activeElement\)/,
    "containment in the list is too broad — the controls live there too");
  assert.match(script, /closest\(["']li\.row["']\)/,
    "defer only while the keyboard is on a row, which is what the deferral was for");
});
