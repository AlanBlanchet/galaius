/** "We can't see IN/OUT when an agent uses a command like in claude code in a box."
 *
 *  A tool call and its result rendered as two unrelated blocks: a wrench and a `<pre>` of arguments,
 *  then somewhere below, a separate block of output. Nothing said the second was the answer to the
 *  first, and nothing labelled which was which. Claude Code shows one box: the command that went
 *  IN and what came back OUT.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { renderTranscript } from "./conversationFormat.ts";

const call = (tool: string, input: string) => ({ kind: "tool", tool, tool_input: input });
const result = (text: string) => ({ kind: "tool_result", text });

test("the command itself is shown verbatim", () => {
  const html = renderTranscript([call("Bash", "grep -rn 'x' src/"), result("ok")] as never[]);
  assert.ok(html.includes("grep -rn &#39;x&#39; src/"), "the command must be readable, and escaped");
});

test("a call still awaiting its answer is a box with only IN", () => {
  /* The common live case: you are watching it work. */
  const html = renderTranscript([call("Bash", "sleep 30")] as never[]);
  assert.match(html, />IN</);
  assert.ok(!/>OUT</.test(html), "there is no answer yet; claiming one would be a lie");
});

test("a result with no call of its own still renders", () => {
  /* Transcripts get truncated; an orphan result must not vanish. */
  const html = renderTranscript([result("something came back")] as never[]);
  assert.ok(html.includes("something came back"));
});

test("two commands in a row do not swallow each other's answers", () => {
  const html = renderTranscript([
    call("Read", "a.ts"), result("contents of a"),
    call("Read", "b.ts"), result("contents of b"),
  ] as never[]);
  const boxes = html.match(/<details class="turn turn-tool[^"]*"/g) ?? [];
  assert.equal(boxes.length, 2);
  assert.ok(html.indexOf("contents of a") < html.indexOf("b.ts"), "answers must stay with their call");
});

test("machine output inside the box is never parsed as markdown", () => {
  /* `# comment` is a shell comment and a pipe table is some command's output. */
  const html = renderTranscript([call("Bash", "cat x"), result("# not a heading\n| not | a table |")] as never[]);
  assert.ok(!html.includes("<h1"), "tool output was parsed as prose");
  assert.ok(!html.includes("<table"), "tool output was parsed as prose");
});

test("everything in the box is escaped", () => {
  const html = renderTranscript([
    call("Bash", "<img src=x onerror=alert(1)>"), result("</pre><script>bad()</script>"),
  ] as never[]);
  assert.ok(!html.includes("<img"), "the command is not escaped");
  assert.ok(!html.includes("<script>bad"), "the output is not escaped");
});
