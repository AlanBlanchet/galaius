/** The two panels must not be able to say the same fact two different ways.
 *
 *  They already shared sprites, palette and pod colours and STILL diverged: the desk stamped the
 *  word ERROR across a work order while the building signalled the identical state with a 6px
 *  wedge and no word anywhere, and the board by the door called that same state "stuck" while the
 *  desk called it ERROR. Nothing failed. Nothing could fail — a second vocabulary is not a bug any
 *  renderer can trip over, it is two files quietly disagreeing, which is exactly how the pod hues
 *  drifted before this.
 *
 *  So the guard is structural rather than visual: ONE module owns the words, ONE stylesheet rule
 *  owns the device, and neither view is allowed to hold a copy. A visual critic can see that the
 *  two panels look alike today; only this can stop them drifting apart tomorrow.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { STATUS } from "./statusLanguage.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...p: string[]) => readFileSync(join(here, "..", ...p), "utf8");

// Same reason as workplaceView.test.ts: the sources import each other without file extensions,
// which node's type-stripping loader cannot resolve, so the built bundle is what gets exercised.
const wp = createRequire(import.meta.url)("../out/workplace.js");

const SCENE = read("webview", "workplace", "scene.ts");
const SPINDLE = read("webview", "sidebar-proto", "spindle.ts");
const WP_STYLE = read("webview", "workplace", "style.ts");
const SP_STYLE = read("webview", "sidebar-proto", "style.ts");

test("one rule decides what a run is showing, for every state either panel can be in", () => {
  const cases: Array<[string, number, string | null]> = [
    ["running", 0, null],
    ["running", 119, null],
    // Two minutes of nothing is HELD — the boundary is inclusive, and it is the same boundary on
    // both surfaces because there is only one of it now.
    ["running", 120, "HELD"],
    ["running", 9999, "HELD"],
    ["done", 0, "DONE"],
    // Only WORK can stall. A finished run's idle clock is how long ago it ENDED; the building used
    // to snooze it and the desk did not, which is the two panels contradicting each other about
    // one person.
    ["done", 9999, "DONE"],
    ["foreign", 0, "NOT OURS"],
    ["foreign", 9999, "NOT OURS"],
    ["error", 0, "ERROR"],
    ["error", 9999, "ERROR"],
  ];
  for (const [status, idle, word] of cases) {
    const got = wp.stampFor({ status, idle_seconds: idle });
    assert.equal(got?.word ?? null, word, `${status} idle=${idle}`);
    assert.equal(wp.isHeld({ status, idle_seconds: idle }), word === "HELD");
  }
});

test("every stamped state has a mark of its OWN — shape carries status before colour does", () => {
  const marks = Object.values(wp.STAMPS).map((s: any) => s.mark);
  assert.equal(new Set(marks).size, marks.length, `two states share one silhouette: ${marks}`);
  // HELD used to be drawn with the open square, which is the mark for NOT OURS.
  assert.notEqual(wp.STAMPS.held.mark, wp.STAMPS["not-ours"].mark);
  // The KEYS are the rail's own `Attention` values, not a private four-state enum beside a
  // six-state one. A world that spells the state `foreign` while the rail spells it `not-ours` is
  // the same two-vocabularies defect one level down from the words.
  for (const kind of Object.keys(wp.STAMPS)) {
    assert.ok(kind in STATUS, `${kind} is not a state the rail knows`);
    assert.equal(wp.STAMPS[kind].word, (STATUS as any)[kind].word, `${kind} spells its own word`);
    assert.equal(wp.STAMPS[kind].accent, (STATUS as any)[kind].accent, `${kind} picks its own colour`);
  }
  for (const kind of Object.keys(wp.STAMPS)) {
    const html = wp.stampHtml(wp.STAMPS[kind]);
    assert.match(html, /<svg/, `${kind} draws no mark`);
    assert.match(html, new RegExp(`data-kind="${kind}"`));
  }
});

test("both stylesheets embed the SHARED stamp rule, and neither writes its own", () => {
  for (const [name, css] of [["workplace", WP_STYLE], ["side bar", SP_STYLE]] as const) {
    assert.ok(css.includes("${STAMP_CSS}"), `${name} does not embed the shared stamp rule`);
    assert.ok(
      !/^\s*\.wp-stamp\s*\{/m.test(css),
      `${name} declares its own .wp-stamp rule — that is the second copy that drifts`,
    );
    assert.ok(!css.includes(".sp-stamp"), `${name} still carries the old private stamp class`);
  }
  // And the rule itself is a real rule, not an empty string that would satisfy the check above.
  assert.match(wp.STAMP_CSS, /\.wp-stamp\s*\{/);
  assert.match(wp.STAMP_CSS, /transform:\s*rotate\(-7deg\)/);
});

test("neither view spells a status word for itself", () => {
  // The words live in ONE table. A literal here is a second vocabulary being born.
  const WORDS = [/"DONE"/, /"ERROR"/, /"NOT OURS"/, /"HELD"/, /"stuck"/, /"finished"/, /idle 2m\+/];
  for (const [name, src] of [["scene.ts", SCENE], ["spindle.ts", SPINDLE]] as const) {
    for (const w of WORDS) {
      assert.ok(!w.test(src), `${name} hard-codes ${w} instead of taking it from status.ts`);
    }
    assert.match(src, /from "(\.\.\/workplace|\.)\/status"/, `${name} does not import the lexicon`);
  }
});

test("the threshold is declared once", () => {
  assert.equal(wp.STALL_SECONDS, 120);
  for (const [name, src] of [["scene.ts", SCENE], ["spindle.ts", SPINDLE]] as const) {
    assert.ok(
      !/const STALL_SECONDS\s*=/.test(src),
      `${name} keeps a private copy of the stall threshold`,
    );
  }
});
