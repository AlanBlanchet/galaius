/** Rendering an agent's transcript — the pure half, so it is testable without VS Code.
 *
 *  This is a CONVERSATION, not a log dump: the model's turns, the tools it called with their
 *  arguments, and what came back. Reading `visual-critic` work should feel like reading the chat,
 *  which is why each kind gets its own shape rather than one uniform grey line.
 *
 *  Everything from an agent is UNTRUSTED text — a tool result can contain arbitrary bytes from a
 *  file or a web page — so it is escaped here, once, rather than at each call site.
 */

import type {
  AgentEvent,
  AgentRun,
  ConversationCatalog,
  ConversationInteraction,
  ConversationRoute,
  InteractionSubmission,
} from "./generated/types";
import type { BillingPresentation } from "./billingPresentation";
import type { TeamSpend } from "./teamSpend";

export interface Turn {
  kind: string;
  text?: string;
  /** Provider lifecycle for events whose kind spans more than one phase, notably child spawn. */
  status?: AgentEvent["status"];
  tool?: string | null;
  tool_input?: string;
  /** The vendor's tool_use id — what lets the ⧉ open this call's WHOLE input/output out of the
   *  raw stream (the summary above is clipped by design). Absent on records that predate it. */
  tool_id?: string;
  /** A command the panel can run, passed IN rather than imported: this module is loaded directly
   *  by its test under --experimental-strip-types, which needs `.ts` specifiers that tsc refuses
   *  when emitting, so it stays import-free. The list lives in `chatCommands.ts`. */
  /** For a message: who sent it. "operator" is a person, anything else is another agent. */
  from_run?: string | null;
  to_run?: string | null;
}

/* ── the small subset of Markdown agents actually write ────────────────────────────────────────
 *
 *  Agents write in Markdown by habit — bold for emphasis, backticks for a path, a dash list for
 *  findings — and the chat printed it literally, so a verdict arrived as a wall of asterisks.
 *
 *  SAFETY: these run on ALREADY-ESCAPED text, so by the time a rule matches there is no angle
 *  bracket or ampersand left and nothing here can produce a tag the agent chose — the rules only
 *  INSERT markup around text that is already inert. A link is the one exception that carries a
 *  value into an attribute, so its href is restricted to http(s).
 *
 *  Lives here rather than in its own module because this repo emits CommonJS from tsc while the
 *  test runner resolves ESM: a cross-file value import would need a .ts specifier that tsc
 *  rejects while emitting. Only this file uses it. */

const FENCE = /```[a-z]*\n([\s\S]*?)```/g;
const CODE = /`([^`\n]+)`/g;
const BOLD = /\*\*([^*\n]+)\*\*/g;
// The delimiter must hug its text: "2 * 3 * 4" is arithmetic, not emphasis.
const ITALIC = /(^|[\s(])[*_](\S[^*_\n]*?\S|\S)[*_](?=[\s).,;:!?]|$)/g;
const LINK = /\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g;

/** Already-escaped text in, HTML out. Never call this on raw agent output. */
export function renderInline(escaped: string): string {
  return escaped
    .replace(LINK, (_m, text, href) => `<a href="${href.replace(/"/g, "&quot;")}">${text}</a>`)
    .replace(BOLD, "<strong>$1</strong>")
    .replace(ITALIC, "$1<em>$2</em>")
    .replace(CODE, "<code>$1</code>");
}

/** Prose: the block constructs an agent actually writes. Input must already be escaped.
 *
 *  It used to know paragraphs and bullets, so everything else arrived as literal punctuation — a
 *  verdict headed "## Verdict" printed its hashes, a numbered plan printed its numbers, a results
 *  table printed its pipes. Agents write Markdown by habit; a chat that renders four of its
 *  constructs is a chat that renders none of the interesting ones.
 *
 *  Line-based and deliberately small: no nesting, no reference links, no HTML passthrough (the
 *  input is escaped and MUST stay that way — every byte here came out of a file or a web page).
 *  Note the escaping when matching: a blockquote marker arrives as `&gt;`, not `>`.
 */
function renderProseBlocks(escaped: string): string[] {
  const out: string[] = [];
  const lines = escaped.split("\n");

  /** A run of list items, closed when anything else appears. */
  let list: string[] = [];
  let ordered = false;
  const flushList = () => {
    if (list.length) {
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${list.map((li) => `<li>${renderInline(li)}</li>`).join("")}</${tag}>`);
    }
    list = [];
  };

  /** A run of quoted lines, kept together so a multi-line quote is ONE block. */
  let quote: string[] = [];
  const flushQuote = () => {
    if (quote.length) {
      out.push(`<blockquote>${quote.map((q) => `<p>${renderInline(q)}</p>`).join("")}</blockquote>`);
    }
    quote = [];
  };

  const flush = () => { flushList(); flushQuote(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // A table is the one construct that needs lookahead: a header row is only a header because the
    // NEXT line is a separator. Without that check a prose line containing a pipe becomes a table.
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] ?? "")) {
      flush();
      const cells = (row: string) =>
        row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      const body: string[][] = [];
      let j = i + 2;
      for (; j < lines.length && /^\s*\|.*\|\s*$/.test(lines[j]); j++) body.push(cells(lines[j]));
      out.push(
        `<table><thead><tr>${head.map((c) => `<th>${renderInline(c)}</th>`).join("")}</tr></thead>` +
        `<tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`,
      );
      i = j - 1;
      continue;
    }

    const heading = /^\s*(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      const level = heading[1].length;
      out.push(`<h${level} class="md">${renderInline(heading[2].trim())}</h${level}>`);
      continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      out.push("<hr>");
      continue;
    }

    // `&gt;` because the input is already escaped.
    const quoted = /^\s*&gt;\s?(.*)$/.exec(line);
    if (quoted) {
      flushList();
      quote.push(quoted[1]);
      continue;
    }

    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      flushQuote();
      if (!ordered) flushList();
      ordered = true;
      list.push(numbered[1]);
      continue;
    }

    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushQuote();
      if (ordered) flushList();
      ordered = false;
      list.push(bullet[1]);
      continue;
    }

    flush();
    out.push(line.trim() ? `<p>${renderInline(line)}</p>` : "");
  }
  flush();
  return out.filter(Boolean);
}

/** A whole message body: fenced blocks, then prose. Input must already be escaped.
 *
 *  Split rather than substituted. The first version swapped each fenced block for a placeholder
 *  and put it back afterwards, which needed a character that could not occur in the text — and
 *  the one I reached for was a NUL, which is legal inside a template literal, survives the
 *  compiler, passes every test, and quietly makes the whole source file BINARY to grep and to
 *  anything else that asks what type it is. `split` on a capturing regex gives alternating prose
 *  and code with nothing to smuggle. */
/** A message body as its top-level BLOCKS: fenced code and rendered prose, in order.
 *
 *  Exposed as blocks because folding a long message has to happen HERE, on parsed output, not on
 *  the raw text. Cutting the source at a line count and escaping the tail meant everything past the
 *  cut was never parsed at all — a table printed its pipes, which is precisely the defect the
 *  parser exists to prevent. Blocks are the unit that can be hidden without destroying meaning.
 */
export function renderMarkdownBlocks(escaped: string): string[] {
  return escaped
    .split(FENCE)
    .flatMap((seg, i) =>
      i % 2 ? [`<pre class="code">${seg.replace(/\n$/, "")}</pre>`] : renderProseBlocks(seg),
    );
}

export function renderMarkdown(escaped: string): string {
  return renderMarkdownBlocks(escaped).join("");
}

/** Escape for HTML text AND attribute contexts. The webview's CSP blocks inline scripts, but a
 *  broken-out tag would still wreck the layout — and a tool result is arbitrary bytes. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** One CSS class per kind, so the eye can separate speech from machinery. An unknown kind falls
 *  back to `other` rather than going unstyled — a future provider event must still render. */
export function turnClass(kind: string): string {
  const known = ["text", "thinking", "tool", "tool_result", "started", "done", "error",
                 "rate_limit", "message", "prompt", "spawn"];
  return `turn turn-${known.includes(kind) ? kind : "other"}`;
}

/** Events that are about the RUN, not the conversation. A "five_hour limit: allowed" rendered
 *  between a tool call and its own result broke the one sequence a reader needs unbroken, so
 *  infrastructure is kept out of the turn stream entirely — it belongs in the header. */
const NOT_A_TURN = new Set(["rate_limit", "started", "done", "other"]);

/** How much of a long block is shown before it folds. The rest is one click away, not gone.
 *
 *  This used to CUT at 14 lines and say how many were hidden, justified by a comment claiming the
 *  panel runs with scripts disabled so expansion "can never be retrofitted". That was stale — the
 *  document carries a nonce'd script — and it was never needed anyway: `<details>` is a native
 *  disclosure that needs no script at all. Truncating a tool result to fourteen lines and offering
 *  no way to see the rest is exactly the wall it was trying to avoid.
 */
const FOLD_AFTER_LINES = 14;
/** Also fold on sheer length: a tool result is frequently ONE enormous line of JSON, which has no
 *  newlines to count and so sailed past the line check and filled the panel. */
const FOLD_AFTER_CHARS = 1200;

/** Escaped HTML for a block, folding the tail into a native disclosure when it is long. */
function foldLongOutput(text: string): string {
  const lines = text.split("\n");
  if (lines.length > FOLD_AFTER_LINES) {
    const head = escapeHtml(lines.slice(0, FOLD_AFTER_LINES).join("\n"));
    const rest = escapeHtml(lines.slice(FOLD_AFTER_LINES).join("\n"));
    const hidden = lines.length - FOLD_AFTER_LINES;
    return `${head}<details class="more"><summary>${hidden} more line${hidden > 1 ? "s" : ""}` +
      `</summary>${rest}</details>`;
  }
  if (text.length > FOLD_AFTER_CHARS) {
    const hidden = text.length - FOLD_AFTER_CHARS;
    return `${escapeHtml(text.slice(0, FOLD_AFTER_CHARS))}` +
      `<details class="more"><summary>${hidden} more characters</summary>` +
      `${escapeHtml(text.slice(FOLD_AFTER_CHARS))}</details>`;
  }
  return escapeHtml(text);
}

/** How many rendered BLOCKS a message shows before the rest collapses. Blocks, not source lines:
 *  a table is one block whether it has three rows or thirty, and hiding half of it would be worse
 *  than hiding all of it. */
const FOLD_AFTER_BLOCKS = 12;

/** Collapse the tail of a long message, on ALREADY-RENDERED blocks.
 *
 *  The bug this replaces cut the RAW markdown at a line count, escaped both halves and only then
 *  handed the result to the parser — so the tail was literal text wrapped in a stray `<details>`
 *  that opened inside a paragraph. Any message over fourteen lines lost its tables, headings and
 *  lists, which is most of what an agent writes.
 */
function foldBlocks(blocks: string[]): string {
  const text = blocks.join("");
  if (blocks.length <= FOLD_AFTER_BLOCKS && text.length <= FOLD_AFTER_CHARS) return text;
  const head = blocks.slice(0, FOLD_AFTER_BLOCKS);
  const rest = blocks.slice(FOLD_AFTER_BLOCKS);
  if (!rest.length) return text;
  const n = rest.length;
  return head.join("") +
    `<details class="more"><summary>${n} more block${n > 1 ? "s" : ""}</summary>` +
    `${rest.join("")}</details>`;
}

const LABEL: Record<string, string> = {
  text: "assistant",
  thinking: "thinking",
  tool_result: "result",
  started: "session",
  done: "finished",
  error: "error",
  rate_limit: "rate limit",
  prompt: "you",
  spawn: "spawned",
};

const SPAWN_LABEL: Partial<Record<NonNullable<AgentEvent["status"]>, string>> = {
  completed: "child finished",
  failed: "child failed",
  provider_failed: "child failed",
  cancelled: "child cancelled",
};

/** Who a message is from, as the reader sees it. Unlabelled, an incoming message looked like
 *  noise — and the point of this panel is being able to tell who said what. */
function messageLabel(turn: Turn): string {
  const from = turn.from_run;
  if (!from || from === "operator") return "you";
  return `from ${from}`;
}

/** The tools that ACT ON A FILE, each with the verb a person would say. */
const FILE_TOOLS: Record<string, string> = {
  Edit: "edited", Write: "wrote", Read: "read", NotebookEdit: "edited",
};

/** The file a summarised tool input names, if any. Python writes `file_path='/x/y.ts' …`. */
function filePathOf(input: string): string | null {
  const m = /file_path='([^']+)'/.exec(input);
  return m ? m[1] : null;
}

/** Image files mentioned in a result — a capture the agent took and saved somewhere. */
function imagePathsOf(text: string): string[] {
  return [...text.matchAll(/(?:^|[\s"'`(])((?:\/[^\s"'`():]+)+\.(?:png|jpe?g|gif|webp))/g)]
    .map((m) => m[1]).slice(0, 4);
}

/** A file change as a CARD, never the code dumped into the transcript.
 *
 *  "how others do it is they show a box, and when we click on it it opens the code diff in the
 *  file." The old rendering printed the tool's raw argument soup — old_string, new_string, whole
 *  written documents — inline, which buried the conversation under source nobody asked to read
 *  there. The card names the verb and the file; the click opens the real thing in the editor,
 *  where code belongs. The result turn for a file tool is a mechanical acknowledgement ("The file
 *  has been updated…") and is folded away entirely.
 */
/** One quoted argument out of a summarised tool input. Python writes old_string='...' with the
 *  content's newlines escaped as literal backslash-n; the clip suffix ("... (+N chars)") rides
 *  inside the quotes and is stripped. */
function argOf(input: string, name: string): string | null {
  // Concatenation, not a template literal: the quote-and-backslash soup inside this pattern
  // defeats the check-css template scanner's lexer and every later comment gets falsely flagged.
  const m = new RegExp(name + "='((?:[^'\\\\]|\\\\.)*)'").exec(input);
  if (!m) return null;
  return m[1].replace(/… \(\+\d+ chars\)$/, "");
}

/** A few lines of a change, coloured the way every diff is coloured.
 *
 *  "The code updated / modifications aren't in color, and show too much instead of a preview." So:
 *  at most cap minus lines and cap plus lines, in the theme's own diff tints, with a count for
 *  what the click holds. The preview is a SCENT of the change, never the change.
 */
function diffPreview(oldText: string | null, newText: string | null, cap = 3): string {
  const split = (t: string) => t.split("\\n").filter((l) => l.trim() !== "");
  const row = (cls: string, sign: string, line: string) =>
    `<div class="${cls}"><span class="diff-sign">${sign}</span>${escapeHtml(line)}</div>`;
  const side = (text: string | null, cls: string, sign: string) => {
    if (!text) return "";
    const lines = split(text);
    const shown = lines.slice(0, cap).map((l) => row(cls, sign, l)).join("");
    const more = lines.length > cap ? `<div class="diff-more">· ${lines.length - cap} more</div>` : "";
    return shown + more;
  };
  const body = side(oldText, "diff-del", "−") + side(newText, "diff-add", "+");
  return body ? `<div class="diff">${body}</div>` : "";
}

/** The arguments' one telling fact — the command, the path, the pattern — for the resting row.
 *  Python already leads the summary with it; this pulls the value out of the quotes. */
function gistOf(input: string): string {
  for (const key of ["command", "file_path", "path", "pattern", "url", "query", "prompt"]) {
    const v = argOf(input, key);
    if (v) return v.split("\\n")[0];
  }
  return input.split("\n")[0];
}

/** A command and what it returned, as ONE box.
 *
 *  "We can't see IN/OUT when an agent uses a command like in claude code in a box." They used to
 *  render as two unrelated blocks — a wrench and some arguments, then, somewhere below, a separate
 *  block of output with nothing saying it was the answer to the first, and nothing naming which
 *  was which. Both halves stay VERBATIM inside a pre: a command is not prose, and tool output is
 *  some program's bytes, where a # line is a shell comment and a pipe-shaped line is not a table.
 */
/** A tool call as ONE resting row — "⌕ Bash · npm test · ✓ 41 lines".
 *
 *  "bash isn't well shown (not enough vertical spacing, too much text shown...)" — the box used
 *  to open with its whole IN and half its OUT inline, so five calls buried the conversation. Now
 *  the row is the transcript's unit: what ran, on what, how it went. Clicking the row PEEKS
 *  (first lines of each half, breathing room, verbatim in <pre>); the ⧉ on each half opens the
 *  WHOLE thing in its own read-only tab — exactly the Claude Code gesture — served from the raw
 *  stream by the vendor's tool id, so nothing is clipped there.
 */
function toolBox(call: Turn, answer: Turn | undefined, path: string | null = null): string {
  const name = escapeHtml(call.tool || "tool");
  const input = (call.tool_input ?? "").trim();
  const out = (answer?.text ?? "").trim();
  const outLines = out ? out.split("\n").length : 0;
  // The verdict at a glance. A missing answer is a call still running, never a silent success.
  const failed = /^\s*(ERROR|error:)|Exit code [1-9]/m.test(out.slice(0, 400));
  const note = !out
    ? '<span class="tool-note tool-live">…</span>'
    : failed
      ? '<span class="tool-note tool-bad">✗</span>'
      : `<span class="tool-note tool-ok">✓${outLines > 3 ? ` ${outLines} lines` : ""}</span>`;
  // ALWAYS rendered, labelled with a word. It was gated on tool_id and drawn as a bare glyph —
  // on the owner's real transcripts (which predate id stamping) the headline feature was
  // therefore INVISIBLE, and where it did draw, a bare ⧉ was indistinguishable from a toggle
  // (both verdicts from the closing critic round). Without an id the tab serves the stored
  // text — clipped by the writer, but everything the record holds, honestly.
  const open = (side: "in" | "out") =>
    `<button class="io-open" data-io="${side}" data-toolid="${escapeHtml(call.tool_id ?? "")}"` +
    ` data-tool="${escapeHtml(call.tool || "tool")}"` +
    ` title="Open the whole ${side === "in" ? "input" : "output"} in its own tab">⧉ open</button>`;
  const half = (tag: string, body: string, side: "in" | "out", cap: number) => {
    const lines = body.split("\n");
    const head = lines.slice(0, cap).join("\n");
    const more = lines.length > cap
      ? `<span class="io-count">+${lines.length - cap} more lines in the tab</span>` : "";
    // The WHOLE stored half rides in an inert template, so the open click can hand the host
    // real content even when the raw stream has no id to look this call up by.
    return `<div class="io"><span class="io-tag">${tag}</span>` +
      `<pre class="io-body">${escapeHtml(head)}</pre>${open(side)}${more}</div>` +
      `<template class="io-full" data-side="${side}">${escapeHtml(body)}</template>`;
  };
  // A capture the command saved is EVIDENCE, and evidence gets a card: name it, make it openable,
  // exactly as a file change does — one language for everything an agent shows you.
  const shots = imagePathsOf(out).map((p) =>
    `<button class="file-open io-shot" data-open="${escapeHtml(p)}" title="Open ${escapeHtml(p)}">` +
    `<span class="file-verb">captured</span><span class="file-name">${escapeHtml(p.split("/").pop() ?? p)}</span></button>`).join("");
  const file = path ? (() => {
    const leaf = path.split("/").pop() ?? path;
    const directory = path.slice(0, path.length - leaf.length);
    const verb = FILE_TOOLS[call.tool ?? ""] ?? "touched";
    return `<span class="file-open" data-open="${escapeHtml(path)}" title="Open ${escapeHtml(path)}">` +
      `<span class="file-verb">${escapeHtml(verb)}</span>` +
      `<span class="file-name">${escapeHtml(leaf)}</span>` +
      `<span class="file-dir">${escapeHtml(directory)}</span></span>`;
  })() : "";
  const preview = path && call.tool !== "Read" ? diffPreview(
    argOf(input, "old_string"), argOf(input, "new_string") ?? argOf(input, "content"),
  ) : "";
  return `<details class="turn turn-tool${path ? " turn-file" : ""}"` +
    ` data-tool-id="${escapeHtml(call.tool_id ?? "")}">` +
    `<summary class="tool-row">` +
    `<span class="tool-glyph">⌕</span><span class="tool-name">${name}</span>` +
    `<span class="tool-gist">${escapeHtml(gistOf(input))}</span>${note}${file}</summary>` +
    `<div class="tool-peek">${preview}` +
    (input ? half("IN", input, "in", 4) : "") +
    // No OUT until there IS one: a command still running has no answer, and drawing an empty one
    // would claim it finished.
    (out ? half("OUT", out, "out", 6) : "") +
    shots +
    `</div></details>`;
}

/** One turn as HTML. A tool call leads with its NAME and carries its arguments beneath, because
 *  the name alone ("used Bash") is the status line the tree already shows. */
export function renderTurn(turn: Turn): string {
  const cls = turnClass(turn.kind);
  if (turn.kind === "tool") {
    return toolBox(turn, undefined);
  }
  if (NOT_A_TURN.has(turn.kind)) return ""; // run infrastructure, not something the agent said
  if (turn.kind === "error") {
    return `<div class="${cls}"><div class="who">error</div>` +
      `<div class="body">${escapeHtml(PROVIDER_FAILURE_NOTICE)}</div></div>`;
  }
  // A harness injection (a stop-hook review, a system reminder) is not something HE said, and
  // rendering it verbatim under "YOU" claims he wrote it. It folds as system machinery.
  // BOTH kinds: what a person or the harness types arrives as "prompt" in the real stream, and
  // "message" only for agent-to-agent mail. Gating on "message" alone meant the fold structurally
  // never fired, and a real stop-hook review rendered verbatim as YOU.
  if ((turn.kind === "message" || turn.kind === "prompt") && /^(Stop hook feedback:|\[Request interrupted|<system-reminder>)/.test((turn.text ?? "").trim())) {
    const words = (turn.text ?? "").trim().split(/\s+/).length;
    return `<details class="turn turn-thinking"><summary>⚙ harness <span class="think-count">${words} words</span>` +
      `</summary><pre class="io-body">${foldLongOutput(turn.text ?? "")}</pre></details>`;
  }
  const label = turn.kind === "message"
    ? escapeHtml(messageLabel(turn))
    : turn.kind === "spawn"
      ? SPAWN_LABEL[turn.status ?? "unknown"] ?? LABEL.spawn
      : LABEL[turn.kind] ?? escapeHtml(turn.kind);
  const raw = turn.text ?? "";
  if (turn.kind === "thinking" && !raw.trim()) {
    // The vendor emits thinking blocks with the CONTENT withheld (signature only) unless its own
    // flag persists them. Dropping the event entirely made reasoning invisible — "3 in data, 0
    // rendered" — which reads as the panel hiding something. A quiet marker is the honest render:
    // it happened, and there is nothing more to show.
    return `<div class="turn turn-thinking think-quiet">💭 thought for a moment</div>`;
  }
  if (!raw.trim()) return "";
  if (turn.kind === "thinking") {
    // A thought is context, not speech: fold it to one dim line — how long, in its own words'
    // first breath — and let the reader open it when they actually want the reasoning.
    const words = raw.trim().split(/\s+/);
    const breath = escapeHtml(words.slice(0, 7).join(" "));
    return `<details class="turn turn-thinking"><summary>💭 ${breath}… <span class="think-count">` +
      `${words.length} words</span></summary><div class="body">${renderMarkdown(escapeHtml(raw))}</div></details>`;
  }
  // Agents write Markdown by habit, and the chat printed it literally — a verdict arrived as a
  // wall of asterisks. Rendered only for what an agent SAYS; a tool result is machine output and
  // stays verbatim in its pre.
  // Machine output folds on its raw text and stays verbatim inside a <pre>; what an agent SAYS is
  // parsed first and folded on the rendered blocks, so nothing past the fold loses its markup.
  const body = turn.kind === "tool_result"
    ? foldLongOutput(raw)
    : foldBlocks(renderMarkdownBlocks(escapeHtml(raw)));
  const tag = turn.kind === "tool_result" ? "pre" : "div";
  // `result-of` marks the result as belonging to the call above it, so the two read as one unit
  // rather than as two unrelated blocks.
  const attach = turn.kind === "tool_result" ? " result-of" : "";
  return `<div class="${cls}${attach}"><div class="who">${label}</div><${tag} class="body">${body}</${tag}></div>`;
}

/** The whole transcript, oldest first — a conversation reads top to bottom, unlike the tree's
 *  newest-first tail. Empty turns are dropped so system noise doesn't pad it out. */
export function renderTranscript(turns: Turn[]): string {
  // A tool result belongs to the call above it, so they are rendered TOGETHER and the result is
  // not emitted again on its own. An orphan result (a truncated transcript) still renders, because
  // dropping output nobody can explain is worse than showing it unattached.
  const answers = new Map<number, Turn>();
  const consumed = new Set<number>();
  const results = new Map<string, number[]>();
  turns.forEach((turn, index) => {
    if (turn.kind === "tool_result" && turn.tool_id) {
      const queue = results.get(turn.tool_id) ?? [];
      queue.push(index);
      results.set(turn.tool_id, queue);
    }
  });
  turns.forEach((turn, index) => {
    if (turn.kind !== "tool") return;
    let answerIndex: number | undefined;
    if (turn.tool_id) answerIndex = results.get(turn.tool_id)?.shift();
    else if (turns[index + 1]?.kind === "tool_result" && !turns[index + 1].tool_id) {
      answerIndex = index + 1;
    }
    if (answerIndex !== undefined) {
      answers.set(index, turns[answerIndex]);
      consumed.add(answerIndex);
    }
  });
  const parts: string[] = [];
  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i];
    if (turn.kind === "tool") {
      const answer = answers.get(i);
      const path = turn.tool && FILE_TOOLS[turn.tool] ? filePathOf(turn.tool_input ?? "") : null;
      parts.push(toolBox(turn, answer, path));
      continue;
    }
    if (consumed.has(i)) continue;
    parts.push(renderTurn(turn));
  }
  const html = parts.filter(Boolean).join("\n");
  return html || '<div class="turn turn-other"><div class="body">No activity recorded yet.</div></div>';
}

/* ── The chat panel ──────────────────────────────────────────────────────────────────────────
 *
 *  The tree answers "what is running" and the conversation tab answers "what happened". Neither
 *  let you SAY anything: the panel was a window, not a conversation. This is the surface that
 *  talks back — the same transcript with a composer under it, living in the side bar beside the
 *  agent list rather than as an editor tab you have to summon.
 *
 *  It lives here, beside the renderer it uses, because the escaping rule must be shared: an
 *  agent's name, its output, and a tool result are all arbitrary bytes from a file or a web page.
 */

/** What an empty panel says. It used to point at "the list above" — a surface that no longer
 *  exists there (the roster moved to the Team tab), so a first-timer stared at 740px of void with
 *  directions to a place that was not on the map. An empty state must be a DOOR, not a caption. */
export const CHAT_EMPTY_HINT = "No conversation open yet.";
/** A recursive diagnostic summary, not another copy of each run's full 300-event transcript. */
export const CONVERSATION_ACTIVITY_EVENT_LIMIT = 24;
/** Keep a large provider team from multiplying DOM and registry reads at every tree level. */
export const CONVERSATION_ACTIVITY_CHILD_LIMIT = 24;

export function boundedConversationActivityChildren<Item>(
  children: readonly Item[],
): { items: Item[]; omitted: number } {
  const omitted = Math.max(0, children.length - CONVERSATION_ACTIVITY_CHILD_LIMIT);
  return { items: children.slice(omitted), omitted };
}

/** A file the panel can open for you — the point of a link rather than a path you copy out. */
export interface ChatFile {
  label: string;
  path: string;
}

/** What a run IS, as opposed to what it has said. */
export interface ChatRun {
  run_id: string;
  kind?: AgentRun["kind"];
  capabilities?: AgentRun["capabilities"];
  provider?: string;
  model?: string | null;
  agent?: string | null;
  task?: string;
  cwd?: string;
  pid?: number | null;
  cost_usd?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  /** How much this run was allowed to do on its own, in the words a person chose it by. Absent
   *  when nobody chose, which is not the same as unrestricted and must not read like it. */
  permission?: { label: string; unrestricted: boolean } | null;
}

/** Presentation state owned by the extension host.  Catalog and event payloads stay the generated
 * Python-owned protocol types; only the few extension-local lifecycle facts live here. */
export interface ConversationConsoleState {
  phase: "loading" | "ready" | "empty" | "starting" | "pending" | "error";
  catalog?: ConversationCatalog;
  active_run_id?: string;
  error?: string;
  approvals?: { run_id: string; event: AgentEvent }[];
}

export const PROVIDER_FAILURE_NOTICE =
  "The provider request failed. See the provider session for details.";

/** Provider approval ids need not be globally unique; their run is part of their identity. */
export function conversationApprovalKey(runId: string, approvalId: string): string {
  return `${runId}\0${approvalId}`;
}

/** Parse one hostile webview value against the provider schema already held by the extension.
 *  The generated wire decoder can prove primitive JSON shapes, but only the pending interaction
 *  can prove which keys, kinds and choice values this provider actually offered. */
export function validatedInteractionSubmission(
  interaction: ConversationInteraction,
  candidate: unknown,
): InteractionSubmission | undefined {
  if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) {
    return undefined;
  }
  const envelope = candidate as Record<string, unknown>;
  const envelopeKeys = Object.keys(envelope);
  if (envelopeKeys.length !== 2
      || envelopeKeys.some((key) => key !== "interaction_id" && key !== "values")
      || envelope.interaction_id !== interaction.id
      || envelope.values === null || typeof envelope.values !== "object"
      || Array.isArray(envelope.values)) {
    return undefined;
  }
  const advertised = new Map(interaction.fields.map((field) => [field.key, field]));
  if (advertised.size !== interaction.fields.length) return undefined;
  const rawValues = envelope.values as Record<string, unknown>;
  const submittedKeys = Object.keys(rawValues);
  if (submittedKeys.some((key) => !advertised.has(key))) {
    return undefined;
  }
  const entries: [string, string | boolean][] = [];
  for (const field of interaction.fields) {
    const present = Object.prototype.hasOwnProperty.call(rawValues, field.key);
    if (!present) {
      if (field.required !== false) return undefined;
      continue;
    }
    const value = rawValues[field.key];
    if (field.kind === "boolean") {
      if (typeof value !== "boolean") return undefined;
    } else {
      if (typeof value !== "string") return undefined;
      if (field.kind === "choice" && !(field.options ?? []).includes(value)) return undefined;
      if (field.kind === "text" && !value.trim()) {
        if (field.required !== false) return undefined;
        continue;
      }
    }
    entries.push([field.key, value]);
  }
  return { interaction_id: interaction.id, values: Object.fromEntries(entries) };
}

function terminalConversationEvent(event: AgentEvent): boolean {
  return event.kind === "done" || event.kind === "error" || event.kind === "cancelled";
}

/** Terminal runs cannot retain an actionable approval button, even if resolution was lost. */
export function conversationApprovalsAfterEvent(
  approvals: NonNullable<ConversationConsoleState["approvals"]>,
  run: AgentRun,
  event: AgentEvent,
): NonNullable<ConversationConsoleState["approvals"]> {
  if (terminalConversationEvent(event)) {
    return approvals.filter((approval) => approval.run_id !== run.run_id);
  }
  if (event.kind !== "interaction_resolved" || !event.event_id) return approvals;
  const resolvedId = event.event_id.split(":", 1)[0];
  return approvals.filter((approval) =>
    approval.run_id !== run.run_id || approval.event.interaction?.id !== resolvedId);
}

/** A child's lifecycle is discrete: only the active root may complete the root turn. */
export function conversationStateAfterEvent(
  state: ConversationConsoleState,
  run: AgentRun,
  event: AgentEvent,
): ConversationConsoleState {
  if (!terminalConversationEvent(event) || run.run_id !== state.active_run_id) return state;
  return {
    ...state,
    phase: "ready",
    active_run_id: undefined,
    error: event.kind === "error" ? PROVIDER_FAILURE_NOTICE : undefined,
  };
}

type ConversationRunSnapshot = Omit<AgentRun, "status"> & {
  status?: AgentRun["status"] | AgentEvent["status"];
};

function canonicalRunStatus(
  status: ConversationRunSnapshot["status"],
): AgentRun["status"] | undefined {
  if (status === "completed") return "done";
  if (status === "provider_failed") return "failed";
  if (status === null || status === "interaction_required" || status === "unknown") return undefined;
  return status;
}

function settledRunStatus(status: AgentRun["status"] | undefined): boolean {
  return status === "done" || status === "failed" || status === "cancelled"
    || status === "crashed" || status === "stopped";
}

/** Provider streams may replay richer start metadata after completion. Enrich the snapshot without
 * letting that stale lifecycle claim resurrect a child; a genuinely newer provider turn may run. */
export function mergeConversationRunSnapshot(
  previous: ConversationRunSnapshot | undefined,
  incoming: ConversationRunSnapshot,
): AgentRun {
  const nextStatus = canonicalRunStatus(incoming.status);
  if (!previous) return { ...incoming, status: nextStatus } as AgentRun;
  const previousStatus = canonicalRunStatus(previous.status);
  const sameTurn = previous.provider_turn_id === incoming.provider_turn_id;
  const staleLifecycle = nextStatus === "starting" || nextStatus === "running" || nextStatus === "waiting";
  if (sameTurn && settledRunStatus(previousStatus) && staleLifecycle) {
    return {
      ...previous,
      ...incoming,
      status: previousStatus,
      finished_at: previous.finished_at,
      exit_code: previous.exit_code,
      cost_usd: previous.cost_usd ?? incoming.cost_usd,
      input_tokens: Math.max(previous.input_tokens ?? 0, incoming.input_tokens ?? 0) || undefined,
      output_tokens: Math.max(previous.output_tokens ?? 0, incoming.output_tokens ?? 0) || undefined,
    } as AgentRun;
  }
  return {
    ...previous,
    ...incoming,
    status: nextStatus ?? previousStatus,
    cost_usd: incoming.cost_usd ?? previous.cost_usd,
    input_tokens: Math.max(previous.input_tokens ?? 0, incoming.input_tokens ?? 0) || undefined,
    output_tokens: Math.max(previous.output_tokens ?? 0, incoming.output_tokens ?? 0) || undefined,
  } as AgentRun;
}

/** A recursive projection for the activity drawer.  The run itself remains the generated wire
 * type; the extension adds only its already-renderable transcript and children. */
export interface ConversationActivityView {
  run: AgentRun;
  current_tool?: string | null;
  transcript: Turn[];
  children: ConversationActivityView[];
  omitted_children?: number;
}

export interface ChatDocument {
  /** The conversation that SPAWNED this one, when there is one.
   *
   *  "go back to the parent if there is one." A sub-agent's transcript is unreadable without the
   *  errand it was sent on — the question lives in the caller's conversation, and until now there
   *  was no way from the answer back to the question. Passed in resolved (id + title) so this
   *  module keeps no runtime import. */
  parent?: { runId: string; title: string } | null;
  /** What the whole team is costing and this agent's share. Passed in for the same reason the
   *  commands are: this module stays import-free so its test can load it directly. */
  spend?: TeamSpend;
  /** What the panel can DO, not just say. Passed in rather than imported so this module stays
   *  import-free — its test loads it directly under --experimental-strip-types, which needs `.ts`
   *  specifiers that tsc refuses when emitting. The list itself lives in `chatCommands.ts`.
   *
   *  REQUIRED, deliberately. It was optional, and the one production caller never passed it — so
   *  the "/" button opened an empty list in every state while both sides passed their own unit
   *  tests. `noUnusedLocals` cannot catch that shape: `CHAT_COMMANDS` IS imported and IS used, to
   *  validate inbound messages, just never handed to the renderer. Making it required moves the
   *  check to the compiler, where forgetting it is impossible rather than merely untested. */
  commands: {
    slash: string;
    title: string;
    detail: string;
    command: string;
    needsAgent: boolean;
  }[];
  /** Per-render nonce: the CSP admits only scripts carrying it, so injected markup cannot run. */
  nonce: string;
  turns: Turn[];
  name: string | undefined;
  status: string | undefined;
  /** A conversation you can only WATCH — one of your own sessions, already steered by you in
   *  its own window. The composer says so instead of pretending to send. */
  readOnly?: boolean;
  /** Everyone on this errand, for the strip above the transcript. From `sessionTabs.ts`;
   *  passed in, never imported, so this module keeps no runtime import. */
  tabs?: Tab[];
  /** The run behind the transcript — its definition, brief, context size and identity. Without
   *  it the panel says what an agent SAID and nothing about what it IS. */
  run?: ChatRun;
  /** Files worth opening: the system prompt, the raw stream, the transcript. */
  files?: ChatFile[];
  /** Who sent this agent out, by name. Half of "its own context" is knowing whose errand it is. */
  sentBy?: string | null;
  /** A message has been delivered and nothing has come back yet. Without this the panel looks
   *  identical whether the agent is thinking or the send silently failed — a reviewer read the
   *  silence as a broken button while the reply was on its way. */
  awaitingReply?: boolean;
  /** Present whenever the local conversation host is available, including before a run exists. */
  console?: ConversationConsoleState;
  /** The selected root and every provider-native child, joined causally by the extension. */
  activity?: ConversationActivityView[];
  /** Shared, path-partitioned billing copy computed by the extension host. */
  billing?: BillingPresentation;
}

/** Just the transcript, for patching into a live view.
 *
 *  The panel used to be re-rendered by replacing the whole document, which threw away the scroll
 *  position and anything half-typed in the composer every time the registry changed — the exact
 *  moments you are watching. The document is built once; this is what gets swapped after.
 */
export function transcriptFragment(
  { turns, name, awaitingReply }: { turns: Turn[]; name: string | undefined; awaitingReply?: boolean },
): string {
  const pending = awaitingReply
    ? `<p class="pending">${escapeHtml(name ?? "the agent")} is answering…</p>`
    : "";
  return renderTranscript(turns) + pending;
}

/** One tab per colleague on this errand. Shape mirrors `sessionTabs.ts`'s `SessionTab`, spelled
 *  structurally rather than imported so this module stays runtime-import-free. */
export interface Tab {
  runId: string;
  label: string;
  depth: number;
  here: boolean;
  root: boolean;
  live: boolean;
  more?: number;
}

/** The strip above the transcript: the way home first, then everyone the session put to work.
 *
 *  "make small tabs in the conversation so we can view how the agent is doing, if it's calling
 *  other agents etc.. And we should be able to come back to the main agent." A live dot is the
 *  whole reason to glance at it — a tab that cannot say "still working" is just navigation.
 */
export function renderTabs(tabs: Tab[]): string {
  if (!tabs.length) return "";
  /* WHOEVER IS WORKING SITS NEXT TO HOME. Measured at his real sidebar width, the fourth tab —
     the live one — was clipped to two letters at the edge. The tab you most need is the one
     still going, so it is ordered there rather than left to the accident of spawn order. */
  const ordered = [
    ...tabs.filter((t) => t.root),
    ...tabs.filter((t) => !t.root && t.live),
    ...tabs.filter((t) => !t.root && !t.live),
  ];
  tabs = ordered.length === tabs.length ? ordered : tabs;
  const one = (t: Tab): string =>
    `<button class="tab" data-run="${escapeHtml(t.runId)}" data-depth="${t.depth}"` +
    `${t.here ? ' data-here="1"' : ""}${t.root ? ' data-root="1"' : ""}` +
    `${t.more ? ' disabled title="' + t.more + ' more on this errand"' : ""}>` +
    (t.root ? '<span class="tab-home">⌂</span>' : "") +
    `<span class="tab-name">${escapeHtml(t.label)}</span>` +
    (t.live ? '<span class="tab-live" title="working"></span>' : "") +
    `</button>`;
  return `<nav class="tabs" aria-label="Agents on this errand">${tabs.map(one).join("")}</nav>`;
}

function routeAvailable(route: ConversationRoute): boolean {
  return route.availability === "available";
}

/** Only an included local session may be preselected. API billing always requires a route click. */
function initialRoute(catalog: ConversationCatalog): ConversationRoute | undefined {
  return catalog.routes.find((route) => route.connection === "local_session" && routeAvailable(route));
}

function routeOption(route: ConversationRoute, selected: boolean): string {
  const connection = route.connection === "local_session" ? "session" : route.connection;
  const label = `${route.label} · ${route.provider} · ${connection}`;
  return `<option value="${escapeHtml(route.id)}"${selected ? " selected" : ""}` +
    `${routeAvailable(route) ? "" : " disabled"}` +
    ` data-available="${routeAvailable(route) ? "1" : "0"}"` +
    ` data-note="${escapeHtml(route.billing_note)}"` +
    ` data-reason="${escapeHtml(route.reason ?? "")}"` +
    ` data-charge="${escapeHtml(route.charge_path)}"` +
    ` data-certainty="${escapeHtml(route.cost_certainty)}"` +
    ` data-default="${escapeHtml(route.default_model ?? "")}">${escapeHtml(label)}</option>`;
}

/** Full policy/authentication reasons remain inspectable even when select labels are clipped. */
function unavailableRouteDisclosure(catalog: ConversationCatalog, open = false): string {
  const unavailable = catalog.routes.filter((route) => !routeAvailable(route));
  if (!unavailable.length) return "";
  const rows = unavailable.map((route) => {
    const connection = route.connection === "local_session" ? "session" : route.connection;
    const availability = route.availability.replace(/_/g, " ");
    const reason = route.reason || "No additional reason was reported.";
    return `<li><b>${escapeHtml(route.provider)} · ${escapeHtml(connection)}</b>` +
      `<span>${escapeHtml(availability)} — ${escapeHtml(reason)}</span></li>`;
  }).join("");
  return `<details class="route-issues"${open ? " open" : ""}>` +
    `<summary>${unavailable.length} unavailable route${unavailable.length === 1 ? "" : "s"}</summary>` +
    `<ul>${rows}</ul></details>`;
}

function emptyRouteState(catalog?: ConversationCatalog): string {
  return '<section class="console-state empty"><b>No runnable routes</b>' +
    '<span>Install or authenticate a supported local session, or explicitly configure an API route.</span>' +
    `${catalog ? unavailableRouteDisclosure(catalog, true) : ""}</section>`;
}

function consolePicker(state: ConversationConsoleState): string {
  if (state.phase === "loading" && !state.catalog) {
    return '<section class="console-state loading" role="status">Loading available routes…</section>';
  }
  if (state.error && !state.catalog) {
    return `<section class="console-state error" role="alert"><span>${escapeHtml(state.error)}</span>` +
      '<button type="button" data-action="reload-conversation">Reload bridge</button></section>';
  }
  if ((state.phase === "starting" || state.phase === "pending") && !state.catalog) {
    const message = state.phase === "starting" ? "Starting the conversation…" : "The agent is answering…";
    return `<section class="console-state pending" role="status">${message}</section>`;
  }
  const catalog = state.catalog;
  if (!catalog || catalog.routes.length === 0) return emptyRouteState(catalog);
  if (state.phase === "empty") return emptyRouteState(catalog);
  const selected = initialRoute(catalog);
  const models = catalog.routes.flatMap((route) => (route.models ?? []).map((model) =>
    `<option data-model="${escapeHtml(model.id)}" data-route="${escapeHtml(route.id)}"` +
    ` value="${escapeHtml(model.id)}" label="${escapeHtml(`${model.provider} · ${model.id}`)}"></option>`));
  const criteria = catalog.criteria.map((criterion) =>
    `<option data-criterion="${escapeHtml(criterion)}" value="${escapeHtml(criterion)}"></option>`);
  const status = state.error
    ? `<p id="console-live-state" class="console-state error" role="alert">${escapeHtml(state.error)}</p>`
    : state.phase === "starting"
      ? '<p id="console-live-state" class="console-state pending" role="status">Starting the conversation…</p>'
      : state.phase === "pending"
        ? '<p id="console-live-state" class="console-state pending" role="status">The agent is answering…</p>'
        : '<p id="console-live-state" class="console-state" role="status" hidden></p>';
  const selection = selected?.default_model ?? "";
  const chooseRoute = selected ? "" : '<option value="" selected disabled>Choose a route</option>';
  const selectionDisabled = selected ? "" : " disabled";
  return `<section class="console-picker" aria-label="Conversation route and model">
    <label for="route">Route</label>
    <select id="route" aria-describedby="route-policy billing">${chooseRoute}${catalog.routes.map((route) =>
      routeOption(route, route.id === selected?.id)).join("")}</select>
    <fieldset class="selection-kind"><legend>Choose by</legend>
      <label><input type="radio" name="selection-kind" value="model" checked${selectionDisabled}> Exact model</label>
      <label><input type="radio" name="selection-kind" value="criterion"${selectionDisabled}> Criterion</label>
    </fieldset>
    <label id="selection-label" for="selection">Model</label>
    <input id="selection" list="selection-options" value="${escapeHtml(selection)}"
           autocomplete="off" placeholder="Search the runnable models"${selectionDisabled}>
    <datalist id="selection-options">${models.join("")}</datalist>
    <template id="selection-source">${models.join("")}${criteria.join("")}</template>
    <p id="route-policy" class="route-policy"${selected?.reason ? "" : " hidden"}>` +
      `<b>Provider policy</b><br>${escapeHtml(selected?.reason ?? "")}</p>
    <p id="billing" class="billing"><b>${escapeHtml(selected?.charge_path ?? "Choose a route")}</b>` +
      ` · ${escapeHtml(selected?.cost_certainty ?? "no billing path selected")}<br>` +
      `${escapeHtml(selected?.billing_note ?? "Select a route to inspect its account and billing impact.")}</p>
    ${unavailableRouteDisclosure(catalog)}
    ${status}
  </section>`;
}

function approvalCards(approvals: ConversationConsoleState["approvals"]): string {
  if (!approvals?.length) return "";
  const cards = approvals.flatMap(({ run_id, event }) => {
    const interaction = event.interaction;
    if (!interaction) return [];
    const structured = interaction.kind === "user_input";
    const heading = structured ? "Input requested" : interaction.kind === "file_change_approval"
      ? "File change approval" : interaction.kind === "permission_approval"
        ? "Permission approval" : "Command approval";
    const action = structured ? "Send details" : interaction.kind === "file_change_approval"
      ? "Approve file change" : interaction.kind === "permission_approval"
        ? "Approve permissions" : "Approve command";
    const headingId = `interaction-${run_id.length}-${run_id}-${interaction.id}`;
    let unanswerable = false;
    const fields = interaction.fields.map((field, index) => {
      const required = field.required !== false;
      const attributes = `class="interaction-field" data-field-key="${escapeHtml(field.key)}"` +
        ` data-field-kind="${field.kind}" data-required="${required ? "1" : "0"}"`;
      if (field.kind === "choice") {
        const choices = (field.options ?? []).map((option) =>
          `<label><input type="radio" name="interaction-field-${index}"` +
          ` data-interaction-value value="${escapeHtml(option)}"${required ? " required" : ""}>` +
          ` <span>${escapeHtml(option)}</span></label>`).join("");
        if (required && !choices) unanswerable = true;
        return `<fieldset ${attributes}><legend>${escapeHtml(field.label)}</legend>` +
          (choices || `<span class="interaction-unavailable">No choices were advertised by the provider.</span>`) +
          `</fieldset>`;
      }
      if (field.kind === "text") {
        return `<label ${attributes}><span>${escapeHtml(field.label)}</span>` +
          `<textarea data-interaction-value rows="2"${required ? " required" : ""}></textarea></label>`;
      }
      return `<label ${attributes}><input type="checkbox" data-interaction-value>` +
        ` <span>${escapeHtml(field.label)}</span></label>`;
    }).join("");
    const disclosure = (interaction.disclosure ?? []).length
      ? `<ul class="interaction-disclosure">${interaction.disclosure?.map((item) =>
        `<li><code>${escapeHtml(item)}</code></li>`).join("")}</ul>`
      : "";
    return [`<article class="approval ${structured ? "structured-input" : "approval-command"}"` +
      ` data-run="${escapeHtml(run_id)}" aria-labelledby="${escapeHtml(headingId)}">
      <h3 id="${escapeHtml(headingId)}">${heading}</h3>
      ${event.text && event.text !== "The provider is waiting for your decision."
        ? `<p>${escapeHtml(event.text)}</p>` : ""}
      ${disclosure}
      <form class="interaction-form" data-interaction="${escapeHtml(interaction.id)}"
            data-run="${escapeHtml(run_id)}">
        <div class="interaction-fields">${fields}</div>
        <p class="interaction-error" role="alert" hidden>Complete every required field.</p>
        <button class="interaction-submit" type="submit"${unanswerable ? " disabled" : ""}>${action}</button>
      </form>
    </article>`];
  });
  return cards.length
    ? `<section class="approvals" aria-label="Requests waiting for input">${cards.join("")}</section>`
    : "";
}

function durationOf(run: AgentRun): string | null {
  if (typeof run.started_at !== "number" || typeof run.finished_at !== "number") return null;
  return `${Math.max(0, Math.round(run.finished_at - run.started_at))}s`;
}

function activityNode(view: ConversationActivityView, depth: number): string {
  const { run } = view;
  const requested = run.requested_criterion ?? run.requested_model ?? "provider default";
  const tools = run.tools?.join(", ") || "none reported";
  const facts = [
    ["Task", run.task || "No task reported"],
    ["Route", `${run.provider} · ${run.connection === "local_session" ? "session" : run.connection ?? "unknown"}`],
    ["Requested", requested],
    ["Resolved", run.model ?? "unknown"],
    ["Status", run.status],
    ["Duration", durationOf(run) ?? (run.status === "running" ? "running" : "unknown")],
    ["Current tool", view.current_tool ?? "none"],
    ["Tools", tools],
    ["Usage", `${run.input_tokens ?? "?"} in · ${run.output_tokens ?? "?"} out`],
    ["Billing", `${run.charge_path ?? "unknown"} · ${run.cost_certainty ?? "unknown"}`],
  ];
  const details = facts.map(([label, value]) =>
    `<dt>${escapeHtml(String(label))}</dt><dd>${escapeHtml(String(value))}</dd>`).join("");
  const transcript = view.transcript.length
    ? `<div class="activity-transcript">${renderTranscript(view.transcript)}</div>`
    : '<p class="activity-empty">No additional activity transcript.</p>';
  const omitted = view.omitted_children
    ? `<p class="activity-omitted">${view.omitted_children} earlier child runs omitted from this summary. ` +
      `Open Team or session navigation to inspect every run.</p>`
    : "";
  return `<details class="activity-run" data-run="${escapeHtml(run.run_id)}" data-depth="${depth}"` +
    `>
      <summary><span>${escapeHtml(run.name)}</span><b>${escapeHtml(run.status ?? "unknown")}</b></summary>
      <dl>${details}</dl>${transcript}
      ${view.children.map((child) => activityNode(child, depth + 1)).join("")}${omitted}
    </details>`;
}

function activityTree(
  activity: ConversationActivityView[] | undefined,
  billing: BillingPresentation | undefined,
  spend: TeamSpend | undefined,
): string {
  if (!activity?.length) return "";
  const spendSummary = spend && spend.agents > 0 ? teamSpendSummary(spend) : "";
  const spendRow = spendSummary
    ? `<p class="team-spend" aria-label="Team spend: ${escapeHtml(spendSummary)}">${escapeHtml(spendSummary)}</p>`
    : "";
  const billingSummary = billing
    ? `<p class="billing-summary" aria-label="${escapeHtml(billing.ariaSummary)}"><b>${escapeHtml(billing.heading)}</b>: ` +
      `${billing.lines.map((line) => escapeHtml(line.text)).join("; ")}</p>`
    : "";
  return `<details class="activity-tree"><summary>Agent activity</summary>
    ${spendRow}${billingSummary}${activity.map((view) => activityNode(view, 0)).join("")}
  </details>`;
}

/** The live-patched portion around a transcript.  Keeping it separate lets child/approval events
 * update without replacing the document and destroying a half-written prompt. */
export function conversationActivityFragment(
  activity: ConversationActivityView[] | undefined,
  consoleState: ConversationConsoleState | undefined,
  billing?: BillingPresentation,
  spend?: TeamSpend,
): string {
  const error = consoleState?.error
    ? `<p class="console-state error" role="alert">${escapeHtml(consoleState.error)}</p>`
    : "";
  return error + approvalCards(consoleState?.approvals) + activityTree(activity, billing, spend);
}

export function chatDocument(
  { nonce, turns, name, status, awaitingReply, run, files, sentBy, commands, spend, parent,
    readOnly, tabs, console: consoleState, activity, billing }: ChatDocument,
): string {
  const up = parent
    ? `<button class="back" id="up" data-run="${escapeHtml(parent.runId)}"` +
      ` title="Go to the conversation that started this one">↑ ${escapeHtml(parent.title)}</button>`
    : "";
  // The identity facts a reader keeps needing — which model, what it has cost, its autonomy —
  // used to live in a details block at the TOP of the transcript, a scroll away from wherever you
  // are. They belong in the header, which stays put.
  const facts: string[] = [];
  if (run?.model) facts.push(`<span class="fact" title="model">${escapeHtml(run.model)}</span>`);
  if (run?.cost_usd != null) facts.push(`<span class="fact" title="cost so far">$${run.cost_usd.toFixed(2)}</span>`);
  if (run?.permission) facts.push(`<span class="fact" title="autonomy">${escapeHtml(run.permission.label)}</span>`);
  const header = name
    // "← Close", because that is what it DOES: it leaves this conversation and gives the column
    // back. "← Team" collided with the Team tab's name while landing somewhere else entirely —
    // the label-lie class the professional sweep hunts (ux-critic LOW).
    ? `<header><div class="head-row"><button class="back" id="back" title="Close this conversation">← Close</button>${up}` +
      `<span class="who">${escapeHtml(name)}</span>` +
      `<span class="status">${escapeHtml(status ?? "")}</span>` +
      `<button type="button" class="settings" id="openSettings" title="Interact settings">⚙</button></div>` +
      (facts.length ? `<div class="facts">${facts.join("")}</div>` : "") +
      `</header>`
    : "";
  const pending = awaitingReply
    ? `<p class="pending">${escapeHtml(name ?? "the agent")} is answering…</p>`
    : "";
  const body = name
    ? renderDetails(run, files, sentBy, spend)
    : consoleState
      ? consolePicker(consoleState) +
        `<div id="conversation-activity">${approvalCards(consoleState.approvals)}${activityTree(activity, billing, spend)}</div>`
      : `<div class="hint"><p>${escapeHtml(CHAT_EMPTY_HINT)}</p>` +
        `<button class="door" id="openTeam">Open the Team</button>` +
        `<p class="hint-sub">Pick a character or a roster row there to talk to it.</p></div>`;
  const transcript = name ? renderTranscript(turns) + pending : "";
  const activityAfterTranscript = name
    ? `<div id="conversation-activity">${conversationActivityFragment(activity, consoleState, billing, spend)}</div>`
    : "";
  // The panel could only SEND. Everything else you might want to do with the agent you are
  // reading — stop it, start another, open the team, change workspace — lived in a tree context
  // menu or the command palette. All three reference tools put this behind a slash menu in the
  // panel itself, so this does too, and the same list drives the button.
  const menu = (commands ?? []).map((c) =>
    `<li role="option" data-command="${escapeHtml(c.command)}" data-slash="${escapeHtml(c.slash)}"` +
    `${c.needsAgent && !name ? ' data-needs-agent="1"' : ""}>` +
    `<b>${escapeHtml(c.slash)}</b><span>${escapeHtml(c.title)}</span>` +
    `<i>${escapeHtml(c.detail)}</i></li>`).join("");
  // ALWAYS rendered, agent or no agent. It used to appear only once you had selected somebody,
  // which meant a resting panel offered no controls whatsoever: the tree's title icons are hidden
  // until the pointer enters the header (VS Code shows `.pane-header > .actions` only on
  // hover/focus, and clips the rest — there is no overflow menu), and this half contributed
  // nothing at all. So on a cold start the whole panel had no visible way to reach the team, the
  // workspace switcher, or anything else. Thirteen labelled commands live behind this button; the
  // ones needing an agent already grey themselves out, which is a far better answer than hiding
  // the entire surface.
  // A session interact did not start cannot be steered from here — the CLI refuses the send,
  // and a composer that pretends otherwise is a dead control with a Send button. Watching is
  // the honest offer, and the placeholder says where steering happens.
  const route = consoleState?.catalog ? initialRoute(consoleState.catalog) : undefined;
  const canStart = !name && consoleState?.phase === "ready" && Boolean(route && routeAvailable(route));
  const turnPending = consoleState?.phase === "starting" || consoleState?.phase === "pending";
  const conversationRun = run?.kind === "conversation" || run?.kind === "provider_child";
  const providerChild = run?.kind === "provider_child";
  const resumeUnavailable = run?.kind === "conversation"
    && !run.capabilities?.includes("resume");
  const bridgeStopped = conversationRun && consoleState?.phase === "error";
  const canSend = (Boolean(name) && !readOnly && !providerChild && !resumeUnavailable
    && !turnPending && !bridgeStopped)
    || canStart;
  const coldStart = !name && Boolean(consoleState);
  const readyPlaceholder = coldStart
    ? "Ask the new conversation…  (Shift+Enter for a new line)"
    : `Reply to ${name ?? "the agent"}…  (/ for commands)`;
  const lifecycleLocked = Boolean(readOnly || resumeUnavailable || turnPending || bridgeStopped
    || (coldStart && consoleState?.phase !== "ready"));
  const cancel = turnPending && consoleState?.active_run_id
    ? `<button type="button" id="cancel" data-run="${escapeHtml(consoleState.active_run_id)}">Cancel</button>`
    : "";
  const resumeNotice = resumeUnavailable
    ? `<p class="console-state">This conversation cannot be resumed here. Start a new conversation to continue.</p>`
    : "";
  const composer = `${resumeNotice}<form id="composer" data-cold-start="${coldStart ? "1" : "0"}"
         data-lifecycle-locked="${lifecycleLocked ? "1" : "0"}">
         <ul id="palette" role="listbox" aria-label="Commands" hidden>${menu}</ul>
         <textarea id="message" rows="3" data-ready-placeholder="${escapeHtml(readyPlaceholder)}"
                   ${canSend ? "" : "disabled "}placeholder="${
           canStart || canSend ? escapeHtml(readyPlaceholder)
                : providerChild ? "Inspect here; continue from the parent conversation"
                : readOnly ? "One of your own sessions — watch here, reply in its window"
                : resumeUnavailable ? "This conversation cannot be resumed here"
                : turnPending ? "Waiting for the current turn"
                : consoleState?.phase === "error" ? "The local conversation bridge stopped"
                : coldStart && consoleState?.phase === "ready"
                  ? "Choose a route to start a conversation"
                  : "No runnable conversation route"}"
                   aria-label="${name ? "Message this agent" : "Start a conversation"}"></textarea>
         <div class="controls">
           <button type="button" id="cmds" title="Commands">/</button>
           ${cancel}<button type="submit"${canSend ? "" : " disabled"}>${coldStart ? "Start" : "Send"}</button>
         </div>
       </form>`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>${STYLE}</style>
</head>
<body>
${header}
${renderTabs(tabs ?? [])}
<main id="transcript">${body}<div id="transcript-content">${transcript}</div>${activityAfterTranscript}</main>
${composer}
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
  {
    // Leaving a conversation is one click, and the panel gives the column back to the roster.
    const door = document.getElementById("openTeam");
    if (door) door.addEventListener("click", () => vscode.postMessage({ type: "openTeam" }));
    const back = document.getElementById("back");
    if (back) back.addEventListener("click", () => vscode.postMessage({ type: "back" }));
    const settings = document.getElementById("openSettings");
    if (settings) settings.addEventListener("click", () =>
      vscode.postMessage({ type: "command", command: "interact.openDashboard" }));
    // A tab switches the panel to that colleague's own conversation — and back to the entry
    // agent, which is what the first tab is for.
    document.addEventListener("click", (e) => {
      const tab = e.target && e.target.closest ? e.target.closest(".tab") : null;
      if (!tab || tab.disabled || tab.dataset.here === "1") return;
      vscode.postMessage({ type: "openRun", runId: tab.dataset.run });
    });
    // A tool row PEEKS on click; its ⧉ opens the whole payload in a tab. Delegated, same as the
    // cards below, because the transcript is re-rendered wholesale on every refresh.
    document.addEventListener("click", (e) => {
      const t = e.target;
      if (!t || !t.closest) return;
      const opener = t.closest(".io-open");
      if (opener) {
        e.stopPropagation();
        // The stored text rides along: when the raw stream holds no id for this call (records
        // that predate stamping), the host serves THIS text instead of an empty lookup.
        const box = opener.closest(".tool-peek");
        const tpl = box ? box.querySelector('.io-full[data-side="' + opener.dataset.io + '"]') : null;
        vscode.postMessage({ type: "io", toolId: opener.dataset.toolid,
                             side: opener.dataset.io, tool: opener.dataset.tool,
                             text: tpl && tpl.content.textContent ? tpl.content.textContent : "" });
        return;
      }
    });
    // One rule for every card: a thing with data-open opens where it points. Delegated, because
    // the transcript is re-rendered live and per-element bindings would go stale.
    document.addEventListener("click", (e) => {
      const card = e.target && e.target.closest ? e.target.closest("[data-open]") : null;
      if (card) vscode.postMessage({ type: "open", path: card.getAttribute("data-open") });
    });
    const up = document.getElementById("up");
    if (up) up.addEventListener("click", () => vscode.postMessage({ type: "openRun", runId: up.dataset.run }));
  }

// "about this agent" holds the session id, the working directory and the path to the system
// prompt — the first things wanted when something looks wrong. The document is rebuilt on every
// agent switch, so a <details> reopened by hand collapsed again the moment you moved to the next
// agent: one extra click, every time, for exactly the information a debugging user came for.
// Remembered across renders in the webview's own state, which survives the rebuild.
const details = document.querySelector("details.details");
if (details) {
  const state = vscode.getState() || {};
  if (state.detailsOpen) details.open = true;
  details.addEventListener("toggle", () => {
    vscode.setState({ ...(vscode.getState() || {}), detailsOpen: details.open });
  });
}

// One route choice owns provider + connection mode.  The second control chooses either one exact
// model or one criterion; changing modes rebuilds the searchable datalist from inert DOM nodes.
const form = document.getElementById("composer");
const messageBox = document.getElementById("message");
const routeSelect = document.getElementById("route");
const selectionBox = document.getElementById("selection");
const selectionList = document.getElementById("selection-options");
const selectionSource = document.getElementById("selection-source");
const selectionLabel = document.getElementById("selection-label");
const routePolicy = document.getElementById("route-policy");
const billing = document.getElementById("billing");
function selectionKind() {
  const checked = document.querySelector('input[name="selection-kind"]:checked');
  return checked ? checked.value : "model";
}
function refreshComposerAvailability() {
  if (!form || !messageBox) return;
  const lifecycleReady = form.dataset.lifecycleLocked !== "1";
  const routeReady = form.dataset.coldStart !== "1" || Boolean(
    routeSelect && routeSelect.value && routeSelect.selectedOptions[0]?.dataset.available === "1"
  );
  const enabled = lifecycleReady && routeReady;
  messageBox.disabled = !enabled;
  if (enabled && messageBox.dataset.readyPlaceholder) {
    messageBox.placeholder = messageBox.dataset.readyPlaceholder;
  }
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = !enabled;
}
function refreshSelection(reset) {
  if (!routeSelect || !selectionBox || !selectionList || !selectionSource) return;
  const kind = selectionKind();
  const routeId = routeSelect.value;
  const choices = Array.from(selectionSource.content.querySelectorAll("option"))
    .filter((option) => kind === "criterion"
      ? option.hasAttribute("data-criterion")
      : option.dataset.route === routeId)
    .map((option) => option.cloneNode(true));
  selectionList.replaceChildren(...choices);
  const selectedRoute = routeSelect.selectedOptions[0];
  const routeReady = Boolean(routeSelect.value && selectedRoute?.dataset.available === "1");
  const lifecycleLocked = form?.dataset.lifecycleLocked === "1";
  if (reset) selectionBox.value = kind === "model" ? (selectedRoute?.dataset.default || "") : "";
  selectionBox.placeholder = kind === "model"
    ? "Search the runnable models"
    : "Search model criteria";
  if (selectionLabel) selectionLabel.textContent = kind === "model" ? "Model" : "Criterion";
  selectionBox.disabled = lifecycleLocked || !routeReady;
  document.querySelectorAll('input[name="selection-kind"]').forEach((radio) => {
    radio.disabled = lifecycleLocked || !routeReady;
  });
  routeSelect.disabled = lifecycleLocked;
  if (routePolicy) {
    const reason = routeReady ? (selectedRoute.dataset.reason || "") : "";
    routePolicy.replaceChildren();
    routePolicy.hidden = !reason;
    if (reason) {
      const label = document.createElement("b");
      label.textContent = "Provider policy";
      routePolicy.append(label, document.createElement("br"), reason);
    }
  }
  if (billing) {
    billing.replaceChildren();
    const strong = document.createElement("b");
    strong.textContent = routeReady ? (selectedRoute.dataset.charge || "unknown") : "Choose a route";
    billing.append(strong,
      " · " + (routeReady ? (selectedRoute.dataset.certainty || "unknown") : "no billing path selected"),
      document.createElement("br"),
      routeReady ? (selectedRoute.dataset.note || "")
        : "Select a route to inspect its account and billing impact.");
  }
  refreshComposerAvailability();
}
if (routeSelect) routeSelect.addEventListener("change", () => refreshSelection(true));
document.querySelectorAll('input[name="selection-kind"]').forEach((radio) => {
  radio.addEventListener("change", () => refreshSelection(true));
});
refreshSelection(false);

// One interaction is one transaction. Collect every provider-advertised field, then cross the
// bridge once; changing one input never resolves a request by itself.
document.addEventListener("submit", (event) => {
  const interactionForm = event.target && event.target.matches
    && event.target.matches("form.interaction-form") ? event.target : null;
  if (!interactionForm) return;
  event.preventDefault();
  if (interactionForm.dataset.submitted === "1") return;
  const entries = [];
  let complete = interactionForm.reportValidity();
  interactionForm.querySelectorAll("[data-field-key][data-field-kind]").forEach((field) => {
    const key = field.dataset.fieldKey;
    const kind = field.dataset.fieldKind;
    const required = field.dataset.required === "1";
    const controls = field.querySelectorAll("[data-interaction-value]");
    if (!key || !kind || controls.length === 0) { complete = false; return; }
    if (kind === "choice") {
      const selected = field.querySelector("[data-interaction-value]:checked");
      if (!selected) { if (required) complete = false; return; }
      entries.push([key, selected.value]);
      return;
    }
    const control = controls[0];
    if (kind === "boolean") { entries.push([key, Boolean(control.checked)]); return; }
    if (kind !== "text") { complete = false; return; }
    if (required && !control.value.trim()) { complete = false; return; }
    if (required || control.value.trim()) entries.push([key, control.value]);
  });
  const error = interactionForm.querySelector(".interaction-error");
  if (!complete) { if (error) error.hidden = false; return; }
  if (error) error.hidden = true;
  interactionForm.dataset.submitted = "1";
  interactionForm.querySelectorAll("input, textarea, button").forEach((control) => {
    control.disabled = true;
  });
  vscode.postMessage({ type: "interaction", runId: interactionForm.dataset.run,
    submission: { interaction_id: interactionForm.dataset.interaction,
      values: Object.fromEntries(entries) } });
});
let cancelButton = document.getElementById("cancel");
function refreshCancel(runId) {
  if (!runId) {
    if (cancelButton) cancelButton.remove();
    cancelButton = null;
    return;
  }
  if (!cancelButton) {
    cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.id = "cancel";
    cancelButton.textContent = "Cancel";
    const submit = form ? form.querySelector('button[type="submit"]') : null;
    if (submit) submit.before(cancelButton);
  }
  cancelButton.dataset.run = runId;
  cancelButton.onclick = () => {
    vscode.postMessage({ type: "cancel", runId: cancelButton.dataset.run });
  };
}
refreshCancel(cancelButton ? cancelButton.dataset.run : "");

if (form) {
  const box = messageBox;
  // The box empties optimistically — a chat that lags behind your typing feels broken — but the
  // text is KEPT until the host confirms it went. It used to be discarded on submit, so a send
  // that failed (the agent had ended, the CLI was not on PATH) left you with a toast and no
  // message: you lost what you wrote, which is the one thing a chat box must never do.
  let inFlight = "";
  const send = () => {
    const text = box.value;
    if (!text.trim()) return;
    if (routeSelect && !routeSelect.value) return;
    inFlight = text;
    if (routeSelect && selectionBox) {
      vscode.postMessage({ type: "start", text, routeId: routeSelect.value,
                           selectionKind: selectionKind(), selection: selectionBox.value });
    } else {
      vscode.postMessage({ type: "send", text });
    }
    box.value = "";
    box.setAttribute("data-sending", "1");
  };
  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || (msg.type !== "sent" && msg.type !== "started")) return;
    box.removeAttribute("data-sending");
    if (msg.ok) { inFlight = ""; return; }
    // Put it back exactly as written, and put the cursor where they left it, so the fix is to
    // press Enter again rather than to retype from memory.
    if (inFlight && !box.value) box.value = inFlight;
    inFlight = "";
    box.focus();
  });
  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || msg.type !== "console-state") return;
    const notice = document.getElementById("console-live-state");
    if (notice && typeof msg.message === "string") {
      notice.hidden = !msg.message;
      notice.textContent = msg.message || "";
      if (msg.error && msg.message) {
        const reload = document.createElement("button");
        reload.type = "button";
        reload.dataset.action = "reload-conversation";
        reload.textContent = "Reload bridge";
        notice.append(reload);
      }
      notice.className = "console-state " + (msg.error ? "error" : "pending");
      notice.setAttribute("role", msg.error ? "alert" : "status");
    }
    if (typeof msg.canSend === "boolean") {
      form.dataset.lifecycleLocked = msg.canSend ? "0" : "1";
      if (msg.canSend && messageBox.dataset.readyPlaceholder) {
        messageBox.placeholder = messageBox.dataset.readyPlaceholder;
      } else if (msg.error) {
        messageBox.placeholder = "The local conversation bridge stopped";
      } else if (msg.activeRunId) {
        messageBox.placeholder = msg.message || "Turn in progress";
      }
      refreshSelection(false);
      refreshComposerAvailability();
    }
    if (Object.prototype.hasOwnProperty.call(msg, "activeRunId")) {
      refreshCancel(msg.activeRunId || "");
    }
    const status = document.querySelector("header .status, body > .status");
    if (status && typeof msg.runStatus === "string") status.textContent = msg.runStatus;
  });
  // A slash menu, driven by the same list the markup was built from. Typing "/" opens it, arrows
  // and Enter pick, Escape closes — the shape every one of the reference panels uses.
  const palette = document.getElementById("palette");
  const items = () => Array.from(palette.querySelectorAll("li")).filter((li) => !li.hidden);
  let cursor = 0;
  const paint = () => {
    items().forEach((li, i) => li.setAttribute("aria-selected", String(i === cursor)));
  };
  const openPalette = (typed) => {
    // Double-escaped on purpose: this script lives inside a template literal, so a single
    // backslash is consumed as a string escape and the regex silently becomes /s/ — which split
    // every prefix down to "/" and made the menu appear to ignore what was typed.
    const prefix = (typed || "/").trimStart().split(/\\s/)[0].toLowerCase();
    let any = false;
    palette.querySelectorAll("li").forEach((li) => {
      const match = li.dataset.slash.startsWith(prefix);
      li.hidden = !match;
      any = any || match;
    });
    palette.hidden = !any;
    cursor = 0;
    paint();
  };
  const closePalette = () => { palette.hidden = true; };
  const run = (li) => {
    if (!li || li.dataset.needsAgent) return;
    vscode.postMessage({ type: "command", command: li.dataset.command });
    box.value = "";
    closePalette();
  };
  document.getElementById("cmds").addEventListener("click", () => {
    if (palette.hidden) { openPalette("/"); box.focus(); } else closePalette();
  });
  palette.addEventListener("click", (e) => {
    const li = e.target.closest ? e.target.closest("li") : null;
    if (li) run(li);
  });
  box.addEventListener("input", () => {
    const text = box.value.trimStart();
    if (text.startsWith("/")) openPalette(text); else closePalette();
  });
  // "@" mentions a file. A webview cannot read the workspace, so it asks the extension to offer
  // one and inserts whatever comes back — the same shape all three reference panels use, and the
  // reason briefing an agent about a file no longer means typing a path from memory.
  box.addEventListener("keydown", (e) => {
    if (e.key === "@") {
      e.preventDefault();
      vscode.postMessage({ type: "pickFile" });
    }
  });
  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg && msg.type === "insert" && typeof msg.text === "string") {
      const at = box.selectionStart ?? box.value.length;
      box.value = box.value.slice(0, at) + msg.text + box.value.slice(at);
      box.focus();
      box.selectionStart = box.selectionEnd = at + msg.text.length;
    }
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
  // Enter sends, Shift+Enter makes a new line — the shape every chat box has.
  box.addEventListener("keydown", (e) => {
    if (!palette.hidden) {
      const list = items();
      if (e.key === "ArrowDown") { e.preventDefault(); cursor = (cursor + 1) % list.length; paint(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); cursor = (cursor - 1 + list.length) % list.length; paint(); return; }
      if (e.key === "Escape") { e.preventDefault(); closePalette(); return; }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(list[cursor]); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
}
// A webview cannot open a workspace file itself, so a file button asks the extension to.
// Delegated from the document: the transcript is re-rendered on every registry change, and
// per-node listeners attached at load are lost the moment that happens.
    document.addEventListener("click", (event) => {
      const target = event.target;
      const reload = target && target.closest ? target.closest('[data-action="reload-conversation"]') : null;
      if (reload) { vscode.postMessage({ type: "reload-conversation" }); return; }
      const button = target && target.closest ? target.closest(".file") : null;
  if (!button) return;
  event.preventDefault();
  vscode.postMessage({ type: "open", path: button.getAttribute("data-path") });
});
const main = document.getElementById("transcript");
const transcriptContent = document.getElementById("transcript-content");
// Stick to the bottom only while the reader IS at the bottom: yanking someone back down while
// they are reading further up is worse than not following at all.
function atBottom() {
  return !main || main.scrollHeight - main.scrollTop - main.clientHeight < 40;
}
function follow(wasAtBottom) {
  if (main && wasAtBottom) main.scrollTop = main.scrollHeight;
}
// Later turns arrive as a PATCH, not a reload — a reload would discard the scroll position and
// whatever is half-typed in the composer, which is exactly when you are watching.
window.addEventListener("message", (event) => {
  const msg = event.data;
  if (!msg || msg.type !== "transcript" || !main || !transcriptContent) return;
  const wasAtBottom = atBottom();
  const expandedTools = new Set(Array.from(
    transcriptContent.querySelectorAll("details.turn-tool[open][data-tool-id]"),
    (node) => node.getAttribute("data-tool-id"),
  ).filter(Boolean));
  transcriptContent.innerHTML = msg.html;
  transcriptContent.querySelectorAll("details.turn-tool[data-tool-id]").forEach((node) => {
    if (expandedTools.has(node.getAttribute("data-tool-id"))) node.open = true;
  });
  follow(wasAtBottom);
});
window.addEventListener("message", (event) => {
  const msg = event.data;
  if (!msg || msg.type !== "activity") return;
  const activity = document.getElementById("conversation-activity");
  if (activity) activity.innerHTML = msg.html;
});
follow(true);
// Assigning webview.html creates a new document asynchronously. Provider events can arrive before
// this script is listening, so ask the extension to replay its current finite state once only.
vscode.postMessage({ type: "ready" });
</script>
</body>
</html>`;
}

//: One visual language with the workplace.
//:
//: An independent critic put it plainly: the side bar was "stock VS Code tree, plain text,
//: monospace transcript" while the team tab was "vivid custom pixel art" — "two unrelated
//: products". Both derive their colour from the same `--vscode-*` variables; what differed was
//: the TREATMENT. So the chat borrows the room's own devices: signs are uppercase and letter-
//: spaced on a light plate, panels sit on a hard 2px offset shadow with a solid ink border, and
//: nothing is rounded softer than the pixel grid allows.
const STYLE = `
  /* On BODY, not :root. VS Code injects the live --vscode-* theme variables as an inline style
     on the body element, and :root (the html element) is body's ANCESTOR — so a var() reading them from :root never
     resolves and silently falls back. This panel was therefore permanently dark whatever theme
     the user had chosen, and it looked deliberate. workplace/style.ts scopes its tokens to a
     body DESCENDANT, which is why the same block works there. */
  body {
    --wp-bg: var(--vscode-sideBar-background, var(--vscode-editor-background, #1e1e1e));
    --wp-fg: var(--vscode-editor-foreground, #d4d4d4);
    /* NOT the raw token. VS Code's own Light theme ships descriptionForeground at #767676 on
       #f8f8f8 — 4.28:1 nominal, under the AA floor before this panel touches it — and this is the
       label saying WHO spoke, not decoration. Dark clears it comfortably, which is why it went
       unseen until the light theme first rendered. Pulled toward the foreground, the same
       treatment --wp-ink and --wp-plate already get here. */
    --wp-dim: color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 70%, var(--vscode-editor-foreground, #d4d4d4));
    --wp-ink: color-mix(in srgb, var(--wp-fg) 62%, var(--wp-bg));
    --wp-line: color-mix(in srgb, var(--wp-fg) 26%, var(--wp-bg));
    --wp-wall: color-mix(in srgb, var(--wp-fg) 7%, var(--wp-bg));
    /* Carries background-coloured text, so it is light enough to read against the background. */
    /* 90%, not 82%: the plate carries BACKGROUND-coloured letters at 10-11px, and at 82% the
       pair measured 7.3:1 nominal — which is a good deal less than that once anti-aliasing is
       counted, on text this small. Lighter plate, same device, comfortably readable. */
    --wp-plate: color-mix(in srgb, var(--wp-fg) 90%, var(--wp-bg));
  }
  body { margin: 0; display: flex; flex-direction: column; height: 100vh;
         font-family: var(--vscode-font-family); font-size: var(--vscode-font-size);
         color: var(--wp-fg); background: var(--wp-bg); }

  /* The sign over the door, exactly as a room wears it. */
  .who { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* Nothing in this header may make the document scroll sideways. */
  body { overflow-x: hidden; }
  .back {
    font: inherit; cursor: pointer; padding: 1px 9px; border-radius: 999px; white-space: nowrap;
    /* A parent conversation is titled by its task, which can be a sentence. Bound it here or the
       header scrolls sideways and the way back leaves the screen. */
    max-width: 11em; overflow: hidden; text-overflow: ellipsis; flex: 0 1 auto;
    /* --wp-dim, never the raw token: Light ships descriptionForeground at 4.28:1, under AA. */
    color: var(--wp-dim);
    background: transparent;
    border: 1px solid var(--vscode-panel-border, transparent);
  }
  .back:hover { color: var(--vscode-foreground); background: var(--vscode-list-hoverBackground); }
  .back:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
  /* WRAPS. Capping the one worst child was not enough: "back" + a maxed up-button + the name +
     the status sum to ~420px against ~278px of a 299px sidebar, so the header grew a real
     horizontal scrollbar and pushed the conversation's own name off screen. A header must budget
     its TOTAL width, not its worst element — so the controls hold the first line and the name and
     status fall to the next rather than sliding out of view. */
  header {
    /* Sticky, in the theme's OWN colours. The old header was an inverted plate — a light slab
       whose letters were painted in the panel BACKGROUND colour, which stops contrasting the
       moment a theme moves either colour. Foreground-on-sidebar adapts by construction, and
       sticky means the answers are wherever you are, not a scroll away. */
    position: sticky; top: 0; z-index: 3;
    display: flex; flex-direction: column; gap: .25em;
    margin: 0; padding: .55em .8em .5em;
    background: var(--vscode-sideBar-background, var(--vscode-editor-background));
    color: var(--vscode-foreground);
    border-bottom: 1px solid var(--vscode-panel-border, transparent);
  }
  .head-row { display: flex; flex-wrap: wrap; align-items: center; gap: .35em .5em; }
  .facts { display: flex; flex-wrap: wrap; gap: 4px; }
  .fact {
    font-size: .8em; padding: 1px 7px; border-radius: 999px; color: var(--wp-dim);
    border: 1px solid color-mix(in srgb, var(--vscode-panel-border, #808080) 70%, transparent);
    max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .who { font-weight: 700; letter-spacing: .18em; font-size: 11px; text-transform: uppercase; }
  /* No opacity. (Historical note: the header was once a plate carrying background-coloured
     word composites it toward the plate it sits on — the same way the workplace nameplate and
     three of the sidebar's devices lost their contrast. Said quieter with size and weight, which
     cost nothing legible; see plateContrast.test.ts. */
  /* 11px at 600. Removing the opacity was necessary and not sufficient: at 500/10px the glyphs
     never reach solid ink, so the composited reading stayed near 2.4:1 however good the colour
     pair was. Weight and size are what get a small label over the line. */
  .status { font-size: 11px; font-weight: 600; letter-spacing: .04em; }
  /* An explicit focus ring rather than the UA's outline:auto, whose rendering against a dark
     webview background could not be settled from a static harness — ambiguity is not a thing to
     ship on a keyboard path. */
  textarea:focus-visible, button:focus-visible {
    outline: 2px solid var(--vscode-focusBorder, #4f9cf5); outline-offset: 1px; }

  /* THE ERRAND STRIP. Everyone this session put to work, the way home first. Scrolls sideways
     rather than wrapping: a strip that grows a second row pushes the transcript down every time
     an agent is spawned, which is motion nobody asked for. */
  /* WRAPS, never scrolls. A hidden scrollbar took the live agent off-screen at his real sidebar
     width with no affordance at all — a strip whose whole job is "who is on this errand" cannot
     answer it from behind an invisible edge. Two rows of small pills is cheaper than a lost tab. */
  .tabs {
    display: flex; flex-wrap: wrap; gap: 4px; align-items: center;
    padding: 5px 8px;
    border-bottom: 1px solid var(--vscode-panel-border, transparent);
    background: var(--vscode-editorWidget-background, transparent);
  }
  .tab {
    font: inherit; font-size: .86em; cursor: pointer; flex: none;
    display: inline-flex; align-items: center; gap: 5px;
    padding: 2px 9px; border-radius: 999px; max-width: 22ch;
    color: var(--wp-dim); background: transparent;
    border: 1px solid var(--vscode-panel-border, transparent);
  }
  .tab:hover { background: var(--vscode-list-hoverBackground); color: var(--vscode-foreground); }
  .tab:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
  .tab[data-here="1"] {
    color: var(--vscode-foreground); font-weight: 600;
    background: var(--vscode-list-activeSelectionBackground, var(--vscode-list-hoverBackground));
    border-color: var(--vscode-focusBorder, var(--vscode-panel-border));
  }
  /* Depth is the answer to "is it calling other agents": a helper's helper sits further in. */
  .tab[data-depth="1"] { margin-left: 2px; }
  .tab[data-depth="2"] { margin-left: 10px; }
  .tab[data-depth="3"] { margin-left: 18px; }
  .tab[disabled] { cursor: default; opacity: .7; }
  .tab-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tab-home { opacity: .8; }
  .tab-live {
    width: 6px; height: 6px; border-radius: 50%; flex: none;
    background: var(--vscode-charts-green, #89d185);
    animation: blink 1.6s steps(1) infinite;
  }
  #transcript { flex: 1; overflow-y: auto; padding: .6em .8em; }
  .console-picker {
    display: grid; grid-template-columns: minmax(0, 1fr); gap: .45em;
    padding: .2em 0 .8em;
  }
  .console-picker > label, .selection-kind legend {
    color: var(--wp-dim); font-size: 10px; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase;
  }
  .console-picker select, .console-picker input[list] {
    box-sizing: border-box; min-width: 0; width: 100%; font: inherit;
    color: var(--vscode-input-foreground); background: var(--vscode-input-background);
    border: 1px solid var(--wp-line); border-radius: var(--wp-r-sm); padding: .48em .55em;
  }
  .console-picker select:focus-visible, .console-picker input[list]:focus-visible {
    outline: 2px solid var(--vscode-focusBorder, #4f9cf5); outline-offset: 1px;
  }
  .selection-kind {
    display: flex; flex-wrap: wrap; align-items: center; gap: .35em .8em;
    min-width: 0; margin: .1em 0; padding: 0; border: 0;
  }
  .selection-kind legend { margin-bottom: .3em; }
  .selection-kind label { display: inline-flex; align-items: center; gap: .3em; white-space: nowrap; }
  .billing {
    margin: .2em 0 0; padding: .55em .65em; line-height: 1.4; overflow-wrap: break-word;
    color: var(--wp-dim); background: var(--wp-wall); border-left: 2px solid var(--wp-line);
  }
  .billing b { color: var(--wp-fg); }
  .route-policy {
    margin: .2em 0 0; padding: .55em .65em; line-height: 1.4; overflow-wrap: break-word;
    color: var(--wp-fg); background: var(--vscode-inputValidation-warningBackground, var(--wp-wall));
    border-left: 2px solid var(--vscode-inputValidation-warningBorder, var(--wp-line));
  }
  .route-policy b { font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
  .route-issues { min-width: 0; color: var(--wp-dim); }
  .route-issues summary { cursor: pointer; overflow-wrap: break-word; }
  .route-issues ul { display: grid; gap: .45em; margin: .45em 0 0; padding: 0; list-style: none; }
  .route-issues li { display: grid; gap: .12em; min-width: 0; padding-left: .55em;
                     border-left: 2px solid var(--wp-line); overflow-wrap: break-word; }
  .route-issues li b { color: var(--wp-fg); font-size: .9em; }
  .console-state { display: flex; flex-direction: column; gap: .25em; padding: .8em; }
  .console-state.loading, .console-state.pending { color: var(--wp-dim); }
  .console-state.error {
    color: var(--vscode-errorForeground); border-left: 3px solid var(--vscode-errorForeground);
    background: color-mix(in srgb, var(--vscode-errorForeground, #e06c75) 9%, var(--wp-bg));
  }
  .console-state.empty { color: var(--wp-dim); }
  .console-state.empty b { color: var(--wp-fg); }
  .approvals { display: grid; gap: .55em; margin: .5em 0 .8em; }
  .approval {
    min-width: 0; padding: .5em .55em;
    border: 1px solid var(--wp-line);
    border-radius: var(--wp-r); background: var(--vscode-editorWidget-background, var(--wp-wall));
  }
  .approval > h3 { display: block; max-width: 100%; margin: 0; font-size: 1em; overflow-wrap: anywhere; }
  .approval p { margin: .35em 0 .55em; overflow-wrap: break-word; }
  .interaction-disclosure { margin: .35em 0 .55em; padding-left: 1.25em; overflow-wrap: break-word; }
  .interaction-fields { display: grid; gap: .55em; }
  .interaction-field { display: grid; gap: .3em; min-width: 0; margin: 0; padding: 0; border: 0; }
  fieldset.interaction-field label { display: flex; align-items: center; gap: .35em; }
  .interaction-field > span, .interaction-field legend { font-weight: 600; }
  .interaction-field textarea { box-sizing: border-box; width: 100%; resize: vertical; }
  .interaction-unavailable, .interaction-error {
    color: var(--vscode-errorForeground, var(--wp-fg));
  }
  .interaction-submit { margin-top: .65em; padding: .38em .75em; min-height: 30px; }
  .activity-tree {
    margin: .6em 0 .9em; padding: .55em 0 0; border-top: 1px solid var(--wp-line);
  }
  .activity-tree h2 {
    margin: 0 0 .45em; color: var(--wp-dim); font-size: 10px; letter-spacing: .1em;
    text-transform: uppercase;
  }
  .activity-run {
    margin: .35em 0; padding: .35em .45em; min-width: 0;
    border-left: 2px solid var(--wp-line); background: var(--wp-wall);
  }
  .activity-run .activity-run { margin-left: .55em; background: transparent; }
  .activity-run > summary {
    display: flex; align-items: baseline; justify-content: space-between; gap: .5em;
    cursor: pointer; min-width: 0;
  }
  .activity-run > summary span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .activity-run > summary b { flex: none; color: var(--wp-dim); font-size: .82em; }
  .activity-run dl {
    display: grid; grid-template-columns: minmax(0, auto) minmax(0, 1fr);
    gap: .15em .65em; margin: .5em 0; font-size: .88em;
  }
  .activity-run dt { color: var(--wp-dim); }
  .activity-run dd { min-width: 0; margin: 0; overflow-wrap: break-word; }
  .activity-transcript { margin-top: .55em; padding-top: .5em; border-top: 1px solid var(--wp-line); }
  .activity-empty { margin: .45em 0 0; color: var(--wp-dim); font-style: italic; }
  .activity-omitted { margin: .45em 0; color: var(--wp-dim); overflow-wrap: break-word; }
  .hint { color: var(--wp-dim); padding: 1.2em .9em; }
  .hint-sub { font-size: .88em; }
  .door {
    font: inherit; cursor: pointer; padding: 4px 14px; border-radius: 999px;
    color: var(--vscode-button-foreground, var(--vscode-foreground));
    background: var(--vscode-button-background, transparent);
    border: 1px solid var(--vscode-panel-border, transparent);
  }
  .door:hover { background: var(--vscode-button-hoverBackground, var(--vscode-list-hoverBackground)); }
  .pending { color: var(--wp-dim); font-style: italic; }
  .pending::after { content: ""; animation: blink 1.2s steps(1) infinite; }
  @keyframes blink { 50% { opacity: .4 } }

  /* A command and its answer, as one box.
     "It's too square, corners and integrations" — so this is a soft card rather than a stack of
     right-angled slabs: one rounded container, the two halves separated by a hairline instead of
     a border each, and the whole thing tinted just enough to read as machinery rather than speech. */
  .turn-tool {
    border: 1px solid var(--vscode-panel-border, transparent);
    border-radius: 10px; overflow: hidden;
    background: color-mix(in srgb, var(--vscode-editorWidget-background, transparent) 60%, transparent);
    margin: 0 0 .8em;
  }
  .turn-tool .who { padding: .4em .7em .25em; }
  /* The resting row: what ran, on what, how it went — one line, whole width, breathing room.
     "not enough vertical spacing, too much text shown" — the transcript's unit is this row now;
     the text lives behind it. */
  .tool-row {
    font: inherit; cursor: pointer; display: flex; align-items: baseline; gap: .55em;
    width: 100%; text-align: left; padding: .5em .7em; min-width: 0;
    color: var(--vscode-foreground); background: transparent; border: 0;
  }
  .tool-row:hover { background: var(--vscode-list-hoverBackground); }
  .tool-row:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: -1px; }
  .tool-glyph { flex: none; color: var(--wp-dim); }
  .tool-name { flex: none; font-weight: 600; font-size: .92em; }
  .tool-gist {
    flex: 1 1 auto; min-width: 0; font-family: var(--vscode-editor-font-family); font-size: .88em;
    color: var(--wp-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tool-note { flex: none; font-size: .82em; white-space: nowrap; }
  .tool-ok { color: var(--vscode-charts-green, #89d185); }
  .tool-bad { color: var(--vscode-charts-red, #f48771); font-weight: 700; }
  .tool-live { color: var(--wp-dim); animation: blink 1.2s steps(1) infinite; }
  body.vscode-reduce-motion .tab-live,
  body.vscode-reduce-motion .pending::after,
  body.vscode-reduce-motion .tool-live { animation: none !important; }
  .tool-peek { border-top: 1px solid color-mix(in srgb, var(--vscode-panel-border, #808080) 60%, transparent); }
  .turn-tool .io {
    display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start; gap: .45em; padding: .5em .6em;
  }
  .turn-tool .io + .io { border-top: 1px solid color-mix(in srgb, var(--vscode-panel-border, #808080) 60%, transparent); }
  .turn-tool .io-body { min-width: 0; max-height: 14em; overflow: auto; line-height: 1.45; }
  /* The whole thing, one click away — the Claude Code gesture. */
  .io-open {
    font: inherit; flex: none; cursor: pointer; line-height: 1;
    padding: 2px 7px; border-radius: 4px; color: var(--wp-dim);
    background: transparent; border: 1px solid color-mix(in srgb, var(--vscode-panel-border, #808080) 70%, transparent);
  }
  .io-open:hover { color: var(--vscode-foreground); background: var(--vscode-list-hoverBackground); }
  .io-open:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
  .io-count { flex: none; align-self: flex-end; color: var(--wp-dim); font-size: .78em; white-space: nowrap; }
  .turn-tool .io-tag {
    flex: 0 0 auto; font-size: 9px; font-weight: 700; letter-spacing: .1em;
    padding: 2px 6px; border-radius: 999px; margin-top: .15em;
    color: var(--wp-dim);
    border: 1px solid color-mix(in srgb, var(--vscode-panel-border, #808080) 70%, transparent);
  }
  /* A file change: the same card language as a command, one line, the payload behind the click.
     Full-width button so the whole card is the target, in the editor's own colours. */
  .turn-file { padding: 0; }
  .file-open {
    font: inherit; cursor: pointer; display: flex; align-items: baseline; gap: .5em;
    width: 100%; text-align: left; padding: .45em .7em;
    color: var(--vscode-foreground); background: transparent; border: 0; min-width: 0;
  }
  .file-open:hover { background: var(--vscode-list-hoverBackground); }
  .file-open:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: -1px; }
  .file-verb {
    flex: 0 0 auto; font-size: 9px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
    padding: 2px 6px; border-radius: 999px; color: var(--wp-dim);
    border: 1px solid color-mix(in srgb, var(--vscode-panel-border, #808080) 70%, transparent);
  }
  .file-name { font-family: var(--vscode-editor-font-family); font-size: .92em; font-weight: 600; }
  .file-dir { color: var(--wp-dim); font-size: .8em; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; direction: rtl; min-width: 0; }
  .io-more { padding: 0 .7em .45em 2.6em; }
  .io-more > summary { cursor: pointer; color: var(--wp-dim); font-size: .82em; list-style: none; }
  .io-more > summary::-webkit-details-marker { display: none; }
  /* The diff preview: the theme's own diff tints, monospace, a sign column. */
  .diff { padding: .1em .7em .5em; font-family: var(--vscode-editor-font-family); font-size: .88em; }
  .diff-del, .diff-add { padding: 0 .4em; border-radius: 3px; white-space: pre-wrap; overflow-wrap: break-word; }
  .diff-del {
    background: var(--vscode-diffEditor-removedTextBackground, color-mix(in srgb, var(--vscode-charts-red) 14%, transparent));
    color: var(--vscode-editor-foreground);
  }
  .diff-add {
    background: var(--vscode-diffEditor-insertedTextBackground, color-mix(in srgb, var(--vscode-charts-green) 14%, transparent));
    color: var(--vscode-editor-foreground);
  }
  .diff-sign { display: inline-block; width: 1.1em; color: var(--wp-dim); user-select: none; }
  .diff-more { color: var(--wp-dim); font-size: .85em; padding: 1px .4em; }
  .io-shot { border-top: 1px solid color-mix(in srgb, var(--vscode-panel-border, #808080) 60%, transparent); }
  /* A thought, folded to a whisper. */
  details.turn-thinking > summary { cursor: pointer; list-style: none; color: var(--wp-dim);
    font-style: italic; }
  details.turn-thinking > summary::-webkit-details-marker { display: none; }
  details.turn-thinking .think-count { font-size: .8em; opacity: .8; }
  details.turn-thinking[open] > summary { margin-bottom: .3em; }
  details.turn-thinking .body { color: var(--wp-dim); }
  .think-quiet { color: var(--wp-dim); font-style: italic; font-size: .9em; }
  .turn-tool .io-body {
    flex: 1 1 auto; min-width: 0; margin: 0;
    font-family: var(--vscode-editor-font-family); font-size: .9em;
    white-space: pre-wrap; overflow-wrap: break-word; background: none; border: 0; padding: 0;
  }
  /* Corners and elevation, once.
     Rejected three times as "too square, corners and integrations". The cause was not one hard
     element but a FAMILY: header, speech bubble and button all carried square corners plus a hard
     "2px 2px 0 0" offset — a deliberate comic-plate look — and softening the new tool card alone
     left one round component sitting in an otherwise rectangular, hard-shadowed panel. So the
     radius and the elevation are declared here and used everywhere, and the offset becomes a soft
     low shadow that reads as depth rather than as a printed outline. */
  :root, body {
    --wp-r: 10px;
    --wp-r-sm: 6px;
    --wp-lift: 0 1px 2px color-mix(in srgb, var(--wp-ink) 22%, transparent),
               0 2px 8px color-mix(in srgb, var(--wp-ink) 12%, transparent);
  }
  .turn { margin: 0 0 .7em; line-height: 1.45; }
  .turn .who { font-weight: 600; letter-spacing: .1em; font-size: 10px; text-transform: uppercase;
               color: var(--wp-dim); }
  /* Rendered Markdown. Tight margins: a chat panel is narrow and every blank line costs a line
     of transcript. The symbol fallback is there because an agent's own status glyphs were
     arriving as tofu boxes in a font that has no such block. */
  .turn .body { font-family: var(--vscode-font-family), "Noto Sans Symbols 2", "Segoe UI Symbol",
                sans-serif; }
  .turn .body p { margin: 0 0 .5em; }
  .turn .body p:last-child { margin-bottom: 0; }
  .turn .body ul { margin: .2em 0 .5em; padding-left: 1.1em; }
  .turn .body li { margin: .1em 0; }
  /* The block constructs an agent actually writes. Unstyled, a rendered heading is barely
     distinguishable from the paragraph under it, which is the "not parsed very well" complaint
     surviving the parser. Colour comes from the editor's own foreground, never the raw
     description token — Light ships that at 4.28:1, under AA before we touch it. */
  .turn .body h1.md, .turn .body h2.md, .turn .body h3.md,
  .turn .body h4.md, .turn .body h5.md, .turn .body h6.md {
    margin: .8em 0 .35em; line-height: 1.25; font-weight: 600;
    color: var(--vscode-editor-foreground);
  }
  .turn .body h1.md { font-size: 1.28em; }
  .turn .body h2.md { font-size: 1.16em; }
  .turn .body h3.md { font-size: 1.06em; }
  .turn .body h4.md, .turn .body h5.md, .turn .body h6.md { font-size: 1em; }
  /* A heading that opens a message should not push itself off the top. */
  .turn .body > :first-child { margin-top: 0; }
  .turn .body ol { margin: .35em 0; padding-left: 1.5em; }
  .turn .body ul { margin: .35em 0; padding-left: 1.35em; }
  .turn .body li { margin: .15em 0; }
  .turn .body blockquote {
    margin: .5em 0; padding: .1em 0 .1em .8em;
    border-left: 3px solid var(--vscode-textBlockQuote-border, var(--vscode-panel-border));
    background: var(--vscode-textBlockQuote-background, transparent);
    color: var(--wp-dim);
  }
  .turn .body blockquote p { margin: .25em 0; }
  /* A divider nobody can see is not a divider. One pixel of panel-border was, measured, "barely a
     discernible line" in both themes — so it gets the dim foreground and a little more air, which
     is what makes it read as a deliberate break rather than a rendering artifact. */
  .turn .body hr {
    border: 0; border-top: 1px solid var(--wp-dim); opacity: .6;
    margin: 1.1em 0;
  }
  /* A table must be able to overflow SIDEWAYS on its own rather than widening the panel —
     a 299px sidebar cannot hold a four-column results table, and the document must never
     scroll horizontally. */
  .turn .body table {
    display: block; overflow-x: auto; max-width: 100%;
    border-collapse: collapse; margin: .5em 0; font-size: .95em;
  }
  .turn .body th, .turn .body td {
    border: 1px solid var(--vscode-panel-border, var(--wp-dim));
    padding: .22em .5em; text-align: left; vertical-align: top;
  }
  .turn .body th {
    font-weight: 600; color: var(--vscode-editor-foreground);
    background: var(--vscode-editorWidget-background, transparent);
  }
  .turn .body code { border-radius: 5px; font-family: var(--vscode-editor-font-family); font-size: .92em;
                     background: var(--wp-wall); border: 1px solid var(--wp-line);
                     padding: 0 .25em; }
  .turn .body pre.code { border-radius: 8px; font-family: var(--vscode-editor-font-family); white-space: pre-wrap;
                         overflow-wrap: break-word; background: var(--wp-wall);
                         border: 1px solid var(--wp-line); padding: .4em .6em; margin: .3em 0; }
  .turn .body strong { font-weight: 700; color: var(--wp-fg); }
  .turn .body a { color: var(--vscode-textLink-foreground); }
  .turn-thinking { color: var(--wp-dim); font-style: italic; }
  /* Machine detail RECEDES. It used to wear the same wall-and-ink panel as a spoken message, so
     a tool call and something you actually said were four identical grey plates — and the
     assistant's own prose, which carries no plate at all, was the quietest thing on a screen whose
     whole job is the conversation. The hierarchy was upside down: loudest for the machine,
     nothing for the voice. Now a tool is a quiet inset with a rule down its left, prose is the
     unadorned primary flow, and a spoken turn is the only thing that gets a plate. */
  .turn-tool, .turn-tool_result { font-family: var(--vscode-editor-font-family);
           font-size: .92em; color: var(--wp-dim);
           border-left: 2px solid var(--wp-line); padding: .15em 0 .15em .6em;
           white-space: pre-wrap; overflow-wrap: break-word; }
  .turn-tool .who, .turn-tool_result .who { font-size: 9px; }
  /* A call and its result are ONE thing. The markup has said so since it was written — the result
     carries a result-of class — but no rule ever targeted it, so the two sat a full inter-turn gap apart
     with a broken rule between them, reading as two unrelated blocks. Closing the gap and running
     the rule through is the whole of what the comment always claimed. */
  .turn-tool { margin-bottom: 0; }
  .turn.result-of { margin-top: 0; padding-top: 0; }
  .turn.result-of .who { padding-top: .25em; }
  /* The container's pre-wrap is INHERITED, and the UA stylesheet's own pre{white-space:pre}
     beats an inherited value on the element itself — so a <pre> inside these blocks kept running
     off the panel. At a 300px side bar that cuts a tool's path or result mid-word, which is the
     one surface the tools are here to show. */
  .turn-tool pre, .turn-tool_result pre, .turn .body pre, pre.args {
           white-space: pre-wrap; overflow-wrap: break-word; margin: 0; }
  /* An error is the one turn most worth noticing, and it had nothing but a hue shift on the body
     text — its label stayed the same dim grey as every other label, so it read as ordinary prose.
     Given the same weight as a voice, in the error colour rather than the accent. */
  .turn-error { color: var(--vscode-errorForeground);
           background: color-mix(in srgb, var(--vscode-errorForeground, #e06c75) 9%, var(--wp-bg));
           border: 1px solid color-mix(in srgb, var(--vscode-errorForeground, #e06c75) 38%, var(--wp-bg));
           border-left-width: 3px; padding: .4em .6em; }
  .turn-error .who { color: var(--vscode-errorForeground); }
  /* A VOICE — you, or another agent talking to this one. The one thing on this surface that is
     somebody speaking, so it is the one thing that gets a plate, and it carries the accent down
     its edge so it cannot be mistaken for a machine panel at a glance. */
  .turn-message, .turn-prompt {
           background: color-mix(in srgb, var(--vscode-focusBorder, #4f9cf5) 10%, var(--wp-bg));
           border: 1px solid color-mix(in srgb, var(--vscode-focusBorder, #4f9cf5) 34%, var(--wp-bg));
           border-left-width: 3px;
           border-radius: var(--wp-r); box-shadow: var(--wp-lift); padding: .5em .75em; }
  .turn-message .who, .turn-prompt .who {
           color: color-mix(in srgb, var(--vscode-focusBorder, #4f9cf5) 62%, var(--wp-fg)); }
  /* The fold on a long block: a quiet control, not another plate. */
  .more > summary { cursor: pointer; color: var(--wp-dim); font-size: .92em; padding: .1em 0; }
  .more[open] > summary { margin-bottom: .2em; }

  /* The command menu. Above the box rather than below it: the composer sits at the bottom of the
     panel, so a list that opened downward would be off-screen. */
  #composer { position: relative; }
  .controls { display: flex; gap: .4em; align-self: flex-end; align-items: stretch; }
  #cmds { min-width: 34px; padding: .6em .7em; font-weight: 700; border-radius: 999px;
          background: var(--wp-wall); color: var(--wp-fg); }
  #palette { position: absolute; bottom: 100%; left: 0; right: 0; margin: 0 0 .4em; padding: .2em;
             list-style: none; z-index: 5; max-height: 46vh; overflow-y: auto;
             background: var(--wp-bg);
             border: 1px solid color-mix(in srgb, var(--wp-ink) 40%, transparent);
             border-radius: var(--wp-r); box-shadow: var(--wp-lift); }
  #palette li { display: grid; grid-template-columns: auto 1fr; gap: 0 .5em; padding: .3em .45em;
                cursor: pointer; align-items: baseline; border-radius: var(--wp-r-sm); }
  #palette li b { font-family: var(--vscode-editor-font-family); color: var(--wp-fg); }
  #palette li span { font-weight: 600; }
  #palette li i { grid-column: 2; font-style: normal; font-size: .88em; color: var(--wp-dim); }
  #palette li[aria-selected="true"], #palette li:hover {
             background: var(--vscode-list-activeSelectionBackground, var(--wp-wall)); }
  /* An action that needs an agent, with none open, is shown and disabled rather than hidden — a
     menu whose contents change shape is harder to learn than one with a greyed row. */
  #palette li[data-needs-agent] { opacity: .45; cursor: not-allowed; }

  .details { margin: 0 0 .8em; font-size: .95em; }
  .details summary { cursor: pointer; color: var(--wp-dim); letter-spacing: .04em; }
  /* minmax(0, ...) on both tracks, and break-word rather than 'anywhere': 'anywhere' lets a
     track's MIN-CONTENT width fall to a single character, so the moment the sidebar narrowed —
     which is what happens the instant you click a file link and an editor opens beside it — long
     paths and session ids wrapped one glyph per line. break-word still breaks a value that does
     not fit; it just does not tell the layout the column can be 1ch wide. */
  .details .grid { display: grid; grid-template-columns: minmax(0, auto) minmax(0, 1fr);
                   gap: .15em .8em; margin: .5em 0; }
  .details .k { color: var(--wp-dim); }
  .details .v { overflow-wrap: break-word; min-width: 0;
                font-family: var(--vscode-editor-font-family); }
  /* Narrower than this, two columns cannot both be readable: stack them. */
  @media (max-width: 280px) {
    .details .grid { grid-template-columns: minmax(0, 1fr); gap: 0; }
    .details .k { margin-top: .4em; font-size: 10px; text-transform: uppercase;
                  letter-spacing: .06em; }
    .activity-run dl { grid-template-columns: minmax(0, 1fr); gap: 0; }
    .activity-run dt { margin-top: .35em; font-size: 10px; text-transform: uppercase; }
    .activity-run .activity-run { margin-left: .25em; }
  }
  .details .brief p { margin: .2em 0 .6em; white-space: pre-wrap; }
  .details .files { margin-top: .5em; }
  .details .file { color: var(--vscode-textLink-foreground); overflow-wrap: break-word; }

  #composer { display: flex; flex-direction: column; gap: .4em; padding: .6em .8em;
              border-top: 1px solid var(--wp-line); }
  textarea { resize: vertical; font: inherit; color: var(--vscode-input-foreground);
             background: var(--vscode-input-background);
             border: 1px solid var(--wp-line); border-radius: var(--wp-r); padding: .55em .7em; }
  /* Measured at 55x19px, which is under every published hit-target floor. A mouse-driven desktop
     surface makes that low-stakes rather than harmless — it is still the control this panel exists
     to be used through. */
  button { align-self: flex-end; font: inherit; cursor: pointer;
           border: 1px solid color-mix(in srgb, var(--wp-ink) 40%, transparent);
           border-radius: 999px; box-shadow: var(--wp-lift); padding: .6em 1.3em; min-height: 32px;
           color: var(--vscode-button-foreground); background: var(--vscode-button-background);
           letter-spacing: .08em; text-transform: uppercase; font-size: 11px; font-weight: 600; }
  button:hover { background: var(--vscode-button-hoverBackground); }
  /* Pressed reads as settling, not as a stamp sliding off its own outline. */
  button:active { box-shadow: none; transform: translateY(1px); }
  button:disabled {
    cursor: not-allowed; color: var(--vscode-disabledForeground, var(--wp-dim));
    background: var(--vscode-button-secondaryBackground, var(--wp-wall));
    border-color: var(--wp-line); box-shadow: none; opacity: .55; transform: none;
  }
`;



/** Whether a message has been delivered with nothing back yet.
 *
 *  Derived from the transcript rather than from a flag the send sets, so it survives a reload and
 *  is true for a message sent from anywhere — the composer, the row icon, another agent, the CLI.
 */
export function isAwaitingReply(turns: Turn[]): boolean {
  for (let i = turns.length - 1; i >= 0; i--) {
    if (turns[i].kind === "message") return true;
    // Anything the agent itself produced after the message means it is already answering.
    if (["text", "thinking", "tool", "tool_result", "done", "error"].includes(turns[i].kind)) {
      return false;
    }
  }
  return false;
}

/** A real link that opens the file.
 *
 *  A `command:` URI is VS Code's own mechanism for this and needs no message plumbing — the
 *  earlier attempt posted a message from a click handler, and the handler never fired for reasons
 *  the markup and the compiled bundle both denied. This is documented, and it is also literally
 *  what was asked for: a file link.
 */
function fileLink(file: ChatFile): string {
  const args = encodeURIComponent(JSON.stringify([`file://${file.path}`]));
  // The PATH is shown, not just the label: it is what was actually asked for, it survives a
  // link the workbench declines to follow, and it can be copied into a terminal.
  return (
    `<div class="k">${escapeHtml(file.label)}</div>` +
    `<div class="v"><a class="file" href="command:vscode.open?${args}">` +
    `${escapeHtml(file.path)}</a></div>`
  );
}

/** Compact token counts — "120k" reads at a glance where "120000" does not. */
function tokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
}

/** What the agent IS, above what it has said: its definition, its brief, how much context it has
 *  consumed, and the identity you need to find the process. Collapsed by default — the
 *  conversation is what you came for; this is what you open when you want to know why it behaves
 *  the way it does.
 *
 *  Files are BUTTONS, not links: a webview cannot open a workspace file itself, so each posts a
 *  message and the extension opens it in an editor.
 */
export function renderDetails(
  run: ChatRun | undefined,
  files: ChatFile[] | undefined,
  sentBy?: string | null,
  spend?: TeamSpend,
): string {
  if (!run) return "";
  const rows: [string, string][] = [];
  if (run.agent) rows.push(["definition", run.agent]);
  // Why this one stops to ask and that one just does it — the question a watcher asks about an
  // agent in flight, unanswerable from the record until it was written down. Silence when nobody
  // chose: "default" would state a policy that was never selected.
  if (run.permission) {
    rows.push(["autonomy", run.permission.unrestricted
      ? `${run.permission.label} — acts without asking`
      : run.permission.label]);
  }
  if (sentBy) rows.push(["sent by", sentBy]);
  rows.push(["provider", [run.provider, run.model].filter(Boolean).join(" · ") || "—"]);
  rows.push([
    "context",
    `${tokens(run.input_tokens)} in · ${tokens(run.output_tokens)} out` +
      (run.cost_usd != null ? ` · ~$${run.cost_usd.toFixed(2)}` : ""),
  ]);
  if (run.cwd) rows.push(["working dir", run.cwd]);
  // No pid, no UUID: the professional sweep measured the about-block as half plumbing — two
  // absolute paths wrapped over nine lines each, a dead process number, a full session id nobody
  // can do anything with. What a person needs are the LINKS (rendered as files below); identity
  // for machines stays in the registry.

  const table = rows
    .map(([k, v]) => `<div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v)}</div>`)
    .join("");
  const brief = run.task
    ? `<div class="brief"><div class="k">brief</div><p>${escapeHtml(run.task)}</p></div>`
    : "";
  const links = (files ?? []).length
    ? `<div class="grid files">${(files ?? []).map(fileLink).join("")}</div>`
    : "";
  return `<details class="details"><summary>about this agent</summary>
    <div class="grid">${table}</div>${brief}${links}</details>`;
}

function teamSpendSummary(spend: TeamSpend): string {
  const share = spend.sharePercent != null ? ` · this one ${spend.sharePercent}%` : "";
  const total = spend.total == null ? "Cost not reported" : `~$${spend.total.toFixed(2)}`;
  return `${total} over ${spend.agents} agent${spend.agents > 1 ? "s" : ""}` +
    `${spend.running ? `, ${spend.running} still running` : ""}${share}`;
}
