/** Alan: "The chat panel content is also not parsed very well. We are missing lots of wrappers for
 *  the text to make it show beautifully."
 *
 *  Agents write Markdown by habit — headings to structure a verdict, numbered steps, a quoted line,
 *  a table of results. The renderer knew paragraphs, bullets, fences and inline marks, so every
 *  other construct arrived as literal punctuation: "## Verdict" rendered as the characters "##".
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { renderMarkdown, escapeHtml, renderTurn } from "./conversationFormat.ts";

const md = (s: string) => renderMarkdown(escapeHtml(s));

test("headings become headings, not literal hashes", () => {
  const out = md("## Verdict\n\nIt works.");
  assert.match(out, /<h2[^>]*>Verdict<\/h2>/);
  assert.ok(!out.includes("## "), "the hashes must not survive into the document");
});

test("every heading level a writer actually uses", () => {
  for (const [hashes, tag] of [["#", "h1"], ["###", "h3"], ["####", "h4"]] as const) {
    assert.match(md(`${hashes} Title`), new RegExp(`<${tag}[^>]*>Title</${tag}>`));
  }
});

test("a numbered list is an ordered list", () => {
  const out = md("1. first\n2. second\n3. third");
  assert.match(out, /<ol>/);
  assert.equal((out.match(/<li>/g) ?? []).length, 3);
  assert.ok(!out.includes("1. first"), "the numbering markup must not survive as text");
});

test("bulleted and numbered lists do not bleed into each other", () => {
  const out = md("- alpha\n- beta\n\n1. one\n2. two");
  assert.match(out, /<ul>[\s\S]*<\/ul>[\s\S]*<ol>[\s\S]*<\/ol>/);
});

test("a quoted line is a quote", () => {
  const out = md("> he said this\n\nand then this");
  assert.match(out, /<blockquote>[\s\S]*he said this[\s\S]*<\/blockquote>/);
  assert.ok(!/&gt; he said/.test(out), "the marker must not survive as an escaped angle bracket");
});

test("a horizontal rule is a rule", () => {
  assert.match(md("before\n\n---\n\nafter"), /<hr\s*\/?>/);
});

test("a table renders as a table, with its header", () => {
  const out = md("| Ask | Status |\n| --- | --- |\n| icon | done |\n| zoom | open |");
  assert.match(out, /<table>/);
  assert.match(out, /<th[^>]*>Ask<\/th>/);
  assert.match(out, /<td[^>]*>zoom<\/td>/);
  assert.ok(!out.includes("| --- |"), "the separator row must not render as content");
});

test("inline marks still work inside every new wrapper", () => {
  assert.match(md("## A **bold** heading"), /<h2[^>]*>A <strong>bold<\/strong> heading<\/h2>/);
  assert.match(md("> a **bold** quote"), /<blockquote>[\s\S]*<strong>bold<\/strong>/);
  assert.match(md("1. a `code` step"), /<ol>[\s\S]*<code>code<\/code>/);
});

test("markdown inside a fence is left alone", () => {
  /* A fenced block is machine text: "# not a heading" is a shell comment, not a title. */
  const out = md("```\n# not a heading\n| not | a table |\n```");
  assert.match(out, /<pre class="code">/);
  assert.ok(!out.includes("<h1"), "a fence must not be parsed as prose");
  assert.ok(!out.includes("<table"), "a fence must not be parsed as prose");
});

test("agent output is still escaped inside the new wrappers", () => {
  /* Everything here renders untrusted bytes from a file or a web page. */
  const out = md("## <img src=x onerror=alert(1)>\n\n> <script>bad()</script>");
  assert.ok(!out.includes("<img"), "escaping must survive heading parsing");
  assert.ok(!out.includes("<script>"), "escaping must survive quote parsing");
});

test("a LONG message still gets its markdown parsed past the fold", () => {
  /* The blocking regression: long output was folded by RAW LINE COUNT and escaped before the
     markdown renderer ever saw it, so everything past line 14 arrived as literal text wrapped in a
     stray <details>. A results table printed its pipes — the exact symptom the parser was added to
     fix — for any message longer than fourteen lines, which is most agent verdicts. */
  const long = [
    "## Verdict", "", "Some prose here.", "", "- one", "- two", "- three", "",
    "More prose to push us past the fold.", "", "1. first", "2. second", "3. third", "",
    "### Results", "", "| Surface | Before | After |", "| --- | --- | --- |",
    "| panel | bad | good |", "| world | bad | better |", "", "> a closing quote",
  ].join("\n");
  const out = renderTurn({ kind: "text", text: long } as never);

  assert.match(out, /<table>/, "the table past the fold must be a table, not pipe characters");
  assert.match(out, /<th[^>]*>Surface<\/th>/);
  assert.ok(!out.includes("| Surface |"), "raw pipes must not survive anywhere");
  assert.ok(!out.includes("| --- |"), "the separator row must not survive");
  assert.match(out, /<h3[^>]*>Results<\/h3>/, "a heading past the fold is still a heading");
  assert.match(out, /<blockquote>/, "a quote past the fold is still a quote");
});

test("the fold still folds — a long message is not dumped whole", () => {
  const long = Array.from({ length: 60 }, (_, i) => `paragraph number ${i}`).join("\n\n");
  const out = renderTurn({ kind: "text", text: long } as never);
  assert.match(out, /<details class="more">/, "a very long message must still collapse");
  assert.match(out, /<summary>/);
});

test("a folded tail is valid markup, not a details inside a paragraph", () => {
  const long = Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\n\n");
  const out = renderTurn({ kind: "text", text: long } as never);
  assert.ok(!/<p>[^<]*<details/.test(out), "<details> must not open inside a <p>");
});

test("machine output is still folded verbatim, never parsed", () => {
  /* A tool result is machine text: "# comment" is a shell comment and "| x |" is table-shaped
     output from some command. It must stay literal. */
  const out = renderTurn({ kind: "tool_result", text: Array.from({ length: 30 }, (_, i) => `# out ${i}`).join("\n") } as never);
  assert.ok(!out.includes("<h1"), "a tool result must not be parsed as markdown");
  assert.match(out, /<details class="more">/, "and it must still fold");
});
