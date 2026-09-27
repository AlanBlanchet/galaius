import { strict as assert } from "node:assert";
import { test } from "node:test";

import { escapeHtml, renderMarkdown } from "./conversationFormat.ts";

const md = (raw: string) => renderMarkdown(escapeHtml(raw));

test("bold, italic and code stop arriving as punctuation", () => {
  assert.match(md("**done**"), /<strong>done<\/strong>/);
  assert.match(md("a *quiet* word"), /<em>quiet<\/em>/);
  assert.match(md("run `interact doctor`"), /<code>interact doctor<\/code>/);
});

test("a findings list reads as a list", () => {
  assert.match(md("- first\n- second"), /<ul><li>first<\/li><li>second<\/li><\/ul>/);
});

test("a fenced block keeps its shape", () => {
  assert.match(md("```py\nx = 1\n```"), /<pre class="code">x = 1<\/pre>/);
});

test("an agent cannot inject markup through any of it", () => {
  // The whole reason this runs on escaped text: by the time a rule matches, the angle brackets
  // are already entities, so a rule can only ever wrap text that is already inert.
  const html = md("**<script>alert(1)</script>**");
  assert.ok(!html.includes("<script"), html);
  assert.match(html, /&lt;script&gt;/);
});

test("a link must be http(s), and nothing else becomes one", () => {
  assert.match(md("[docs](https://example.com/x)"), /<a href="https:\/\/example.com\/x">docs<\/a>/);
  const evil = md("[click](javascript:alert(1))");
  assert.ok(!evil.includes("<a "), evil);
});

test("ordinary prose is untouched apart from being wrapped", () => {
  assert.equal(md("just a sentence"), "<p>just a sentence</p>");
});

test("an asterisk that is not emphasis is left alone", () => {
  assert.equal(md("2 * 3 * 4"), "<p>2 * 3 * 4</p>");
});
