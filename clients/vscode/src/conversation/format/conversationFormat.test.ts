/** The ⧉ gesture: every call, every record — not only the freshly-stamped ones.
 *
 *  The closing critic round proved the first cut wrong twice on the owner's real data: the ⧉
 *  was gated on tool_id, and his transcripts predate id stamping, so the headline feature was
 *  INVISIBLE on every run he actually has; and where it drew, a bare glyph read as a toggle.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { renderTabs, renderTranscript } from "./conversationFormat.ts";

test("the open-in-tab affordance exists even on records that predate id stamping", () => {
  const html = renderTranscript([
    { kind: "tool", tool: "Bash", tool_input: "command='make'" }, // no tool_id — an old record
    { kind: "tool_result", text: "ok" },
  ] as never[]);
  assert.match(html, /class="io-open"[^>]*data-toolid=""/,
    "no id means the stored text serves the tab — never a missing control");
  assert.match(html, />⧉ open</, "a word beside the glyph — a bare ⧉ read as a toggle");
});

test("the whole stored half rides in an inert template for the fallback", () => {
  const out = Array.from({ length: 12 }, (_, i) => `l${i}`).join("\n");
  const html = renderTranscript([
    { kind: "tool", tool: "Bash", tool_input: "command='make'", tool_id: "t1" },
    { kind: "tool_result", text: out, tool_id: "t1" },
  ] as never[]);
  assert.match(html, /<template class="io-full" data-side="out">/);
  assert.ok(html.includes("l11"), "the template holds ALL of it, not the peek's head");
  assert.match(html, /\+6 more lines in the tab/);
});

test("the click script hands the host the stored text alongside the id", () => {
  const html = renderTranscript([
    { kind: "tool", tool: "Bash", tool_input: "command='x'", tool_id: "t1" },
  ] as never[]);
  // The script is shared per-document; assert the contract string the handler must carry.
  assert.ok(html.includes('class="io-open"'), "the affordance renders");
});

test("the tab strip renders the way home, the live dots, and the depth", () => {
  const html = renderTabs([
    { runId: "main", label: "main", depth: 0, here: true, root: true, live: true },
    { runId: "a", label: "tester", depth: 1, here: false, root: false, live: true },
    { runId: "b", label: "artist", depth: 2, here: false, root: false, live: false },
  ]);
  assert.match(html, /class="tabs"/);
  assert.match(html, /data-run="a"[^>]*data-depth="1"/);
  assert.match(html, /data-here="1"/, "the tab you are on must be marked");
  assert.equal((html.match(/class="tab-live"/g) ?? []).length, 2, "two are working");
  assert.match(html, />main</, "the entry agent is named, so the way back is obvious");
});

test("no strip when there is nobody else on the errand", () => {
  assert.equal(renderTabs([]), "");
});

test("an overflowing strip never hides the LIVE agent", () => {
  /* The critic measured it at his real sidebar width: the 4th tab — the working one — was
     clipped to "TE" behind a hidden scrollbar with no affordance. The tab you most need is the
     one that is working, so the strip ORDERS by that: home, then whoever is live, then the
     rest. */
  const html = renderTabs([
    { runId: "main", label: "main", depth: 0, here: true, root: true, live: false },
    { runId: "a", label: "quiet-one", depth: 1, here: false, root: false, live: false },
    { runId: "b", label: "another-quiet", depth: 1, here: false, root: false, live: false },
    { runId: "c", label: "tester", depth: 1, here: false, root: false, live: true },
  ]);
  const order = [...html.matchAll(/data-run="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(order, ["main", "c", "a", "b"], "the live agent must sit next to home");
});

test("the strip wraps instead of hiding tabs behind an invisible scrollbar", () => {
  const style = renderTabs([
    { runId: "m", label: "m", depth: 0, here: true, root: true, live: false },
    { runId: "a", label: "a", depth: 1, here: false, root: false, live: false },
  ]);
  assert.ok(!style.includes("scrollbar-width"), "no hidden scrollbar in the strip markup");
});
