/** A backtick inside a comment silently ends the template literal that holds an embedded
 *  stylesheet or script, and everything after it is then parsed as TypeScript. I have done this
 *  six times in one session, and the build fails with a message about an unexpected token hundreds
 *  of lines further down, pointing at code that is fine.
 *
 *  This tests the SAME implementation the build runs (`scripts/check-css-templates.js`) rather
 *  than a second copy of the algorithm. The copy was the earlier design, and the two drifted:
 *  every synthetic case here passed while the real check missed `motion.ts` completely.
 *
 *  Three generations of this guard each shipped a defect the tests below now pin:
 *   - matching only lines that START a comment sailed past a backtick on a middle line;
 *   - stopping at the first closing backtick missed a file whose source is CONCATENATED;
 *   - starting the scan at the first backtick began inside the module docstring and inverted
 *     every state after it, condemning six lines of a file that compiles perfectly.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const { sheets, backticksInCommentedTemplates } = require(
  join(here, "..", "scripts", "check-css-templates.js"),
) as {
  sheets: () => string[];
  backticksInCommentedTemplates: (text: string) => number[];
};

const find = (source: string) => backticksInCommentedTemplates(source);

test("it catches a backtick on the MIDDLE line of a comment — the first version's blind spot", () => {
  assert.deepEqual(find([
    "export const STYLE = String.raw`",
    "  /* a comment that opens here",
    "     and mentions `anywhere` on a line with no asterisk",
    "     and closes here */",
    "  .x { color: red; }",
    "`;",
  ].join("\n")), [3, 3]);
});

test("it catches one on the opening line too", () => {
  assert.deepEqual(find(["const S = `", "  /* uses `break-word` here */", "`;"].join("\n")),
    [2, 2]);
});

test("a backtick in the JSDoc header above the literal is fine", () => {
  // Ordinary prose there — flagging it would make this guard something to switch off.
  assert.deepEqual(find(
    ["/** mixes `--vscode-*` tokens */", "const S = `", "  .x { color: red; }", "`;"].join("\n"),
  ), []);
});

test("it keeps checking after a template closes and another opens", () => {
  // motion.ts's shape: a template, a concatenation, another template. The version that stopped at
  // the first closing backtick checked the first segment and declared the file clean.
  assert.deepEqual(find([
    "export const A = String.raw`",
    "  /* clean */",
    "` + OTHER + String.raw`",
    "  /* mentions `this` */",
    "`;",
  ].join("\n")), [4, 4]);
});

test("a markdown fence in a regex is content, not two template edges", () => {
  // conversationFormat.ts's FENCE. Read as delimiters it inverts every state that follows, which
  // is how a clean file came back with six findings.
  assert.deepEqual(find([
    "const FENCE = /```([\\\\s\\\\S]*?)```/g;",
    "/** prose quoting `something` well after it */",
    "const S = `",
    "  .x { color: red; }",
    "`;",
  ].join("\n")), []);
});

test("a backtick inside a quoted string delimits nothing", () => {
  assert.deepEqual(find([
    'const TICK = "`";',
    "/** prose quoting `something` */",
    "const S = `",
    "  .x { color: red; }",
    "`;",
  ].join("\n")), []);
});

test("a // inside CSS is a selector or a URL, never a comment", () => {
  // Treating it as one would swallow the rest of the line, including a real closing backtick.
  assert.deepEqual(find([
    "const S = `",
    "  .x { background: url(https://example.com/a.png); }",
    "  /* and `this` is a genuine hit after it */",
    "`;",
  ].join("\n")), [3, 3]);
});

test("clean source with comments produces nothing", () => {
  assert.deepEqual(find(["const S = `", "  /* plain words only */", "  .x { red } */", "`;"]
    .join("\n")), []);
});

test("the guard covers every file that carries an embedded source", () => {
  // The list version missed sim.ts — the file that then broke the build. Discovery is what keeps
  // a new embedded source from being unguarded simply because nobody remembered it.
  const covered = sheets();
  // index.ts emits its HTML from a PLAIN template, so a String.raw-only predicate could not
  // see it — and it is the file that broke the build twice in one afternoon on backticks inside
  // its comments. The defect belongs to "comment inside an open template", which no tag confers.
  for (const expected of ["webview/workplace/sim.ts", "webview/workplace/motion.ts",
    "webview/workplace/style.ts", "src/conversationFormat.ts",
    "webview/workplace/index.ts", "src/railHtml.ts"]) {
    assert.ok(covered.some((f) => f.replace(/\\/g, "/").endsWith(expected)),
      `${expected} is not guarded (covered: ${covered.join(", ")})`);
  }
});

test("the shipping sources are clean", () => {
  for (const file of sheets()) {
    const full = join(here, "..", file);
    const hits = find(readFileSync(full, "utf8"));
    assert.deepEqual(hits, [], `${file}: backtick in a comment at line(s) ${hits.join(", ")}`);
  }
});

test("the live reduced-motion body class stops Chat and Team animation clocks", () => {
  const chat = readFileSync(join(here, "conversationFormat.ts"), "utf8");
  const dashboard = readFileSync(join(here, "..", "webview", "styles.css"), "utf8");
  const workplace = readFileSync(join(here, "..", "webview", "workplace", "style.ts"), "utf8");

  assert.match(chat, /body\.vscode-reduce-motion[\s\S]*?animation:\s*none\s*!important/);
  assert.match(dashboard, /body\.vscode-reduce-motion[\s\S]*?animation:\s*none\s*!important/);
  assert.match(workplace, /body\.vscode-reduce-motion[\s\S]*?animation:\s*none\s*!important/);
});

test("an open workplace follows later reduced-motion body class changes", () => {
  const motion = readFileSync(join(here, "..", "webview", "workplace", "motion.ts"), "utf8");

  assert.match(motion, /MutationObserver/);
  assert.match(motion, /vscode-reduce-motion/);
  assert.match(motion, /TICK\.on\s*=\s*!still/);
});
