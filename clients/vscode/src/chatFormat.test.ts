import { strict as assert } from "node:assert";
import { test } from "node:test";

import { chatDocument, CHAT_EMPTY_HINT, isAwaitingReply, transcriptFragment, turnClass } from "./conversationFormat.ts";

// "We have the configuration panel for interact, but not a seperate chat panel !!!!" — the
// transcript existed, but only as a read-only EDITOR tab. A chat panel lives in the side bar
// beside the agent list and lets you TALK to the agent, not just read it.

test("with no agent selected it says what to do rather than rendering blank", () => {
  const html = chatDocument({ nonce: "n1", turns: [], name: undefined, status: undefined });
  assert.match(html, new RegExp(CHAT_EMPTY_HINT));
});

test("the selected agent's name and status head the panel", () => {
  const html = chatDocument({ nonce: "n1", turns: [], name: "reviewer", status: "running" });
  assert.match(html, /reviewer/);
  assert.match(html, /running/);
});

test("the composer is present so the panel can be TALKED to, not only read", () => {
  const html = chatDocument({ nonce: "n1", turns: [], name: "reviewer", status: "done" });
  assert.match(html, /<textarea/);
});

test("a script may only run under the given nonce", () => {
  const html = chatDocument({ nonce: "abc123", turns: [], name: "r", status: "done" });
  assert.match(html, /Content-Security-Policy/);
  assert.match(html, /nonce-abc123/);
  assert.ok(!/<script(?![^>]*nonce="abc123")/.test(html), "every script must carry the nonce");
});

test("an agent name containing markup cannot break out of the header", () => {
  const html = chatDocument({
    nonce: "n1", turns: [], name: '<img src=x onerror="alert(1)">', status: "done",
  });
  assert.ok(!html.includes("<img src=x"), "the name is agent-controlled text, so it is escaped");
  assert.match(html, /&lt;img/);
});

test("a tool result full of markup is escaped too", () => {
  const html = chatDocument({
    nonce: "n1",
    turns: [{ kind: "tool_result", text: "</textarea><script>evil()</script>" }],
    name: "r",
    status: "done",
  });
  assert.ok(!html.includes("<script>evil()"), "tool output is arbitrary bytes from a file or page");
});

// An independent critic sent a message, saw nothing happen for 80s, and reported the composer
// broken. The message HAD been delivered and the reply DID arrive — but with no in-flight state,
// "working" and "broken" look identical, so the panel was lying by omission.

test("while the agent is answering, the panel says so", () => {
  const html = chatDocument({
    nonce: "n1", name: "reviewer", status: "done",
    turns: [{ kind: "message", text: "check the error paths", to_run: "r1" }],
    awaitingReply: true,
  });
  assert.match(html, /answering|waiting/i);
});

test("with no reply pending there is no waiting state", () => {
  const html = chatDocument({
    nonce: "n1", name: "reviewer", status: "done",
    turns: [{ kind: "text", text: "done" }],
    awaitingReply: false,
  });
  assert.ok(!/answering|waiting/i.test(html));
});

test("a delivered message with nothing after it means a reply is pending", () => {
  assert.equal(isAwaitingReply([{ kind: "text", text: "hi" }, { kind: "message", text: "go" }]), true);
});

test("an assistant turn after the message ends the pending state", () => {
  assert.equal(
    isAwaitingReply([{ kind: "message", text: "go" }, { kind: "text", text: "done" }]),
    false,
  );
});

test("tool activity after the message also counts as answering, not silence", () => {
  assert.equal(
    isAwaitingReply([{ kind: "message", text: "go" }, { kind: "tool", tool: "Read" }]),
    false,
  );
});

test("an empty transcript is not pending", () => {
  assert.equal(isAwaitingReply([]), false);
});

// A message rendered with no label and the generic "other" style — present in the data, but
// visually indistinguishable from noise. The whole point of the chat is that you can see who
// said what.

test("a message from a person reads as yours", () => {
  const html = chatDocument({
    nonce: "n", name: "reviewer", status: "done",
    turns: [{ kind: "message", from_run: "operator", to_run: "r1", text: "check the tests" }],
  });
  assert.match(html, /you/i);
  assert.match(html, /check the tests/);
});

test("a message from another agent names that agent", () => {
  const html = chatDocument({
    nonce: "n", name: "reviewer", status: "done",
    turns: [{ kind: "message", from_run: "perf", to_run: "r1", text: "numbers look fine" }],
  });
  assert.match(html, /perf/);
});

test("a message kind is styled as a message, not as the unknown fallback", () => {
  assert.equal(turnClass("message"), "turn turn-message");
});

test("a spawned subagent is labelled as one", () => {
  const html = chatDocument({
    nonce: "n", name: "reviewer", status: "done",
    turns: [{ kind: "spawn", text: "run the suite" }],
  });
  assert.match(html, /spawn/i);
});

// "What i want is to be able to view an active running agent, context, system prompt (a file link
// is enough), basically everything". The panel showed a transcript and nothing about what the
// agent IS — no definition, no brief, no context size, no way to reach the files.

const RUN = {
  run_id: "abcd1234-0000-0000-0000-000000000000",
  name: "code-reviewer", provider: "claude", model: "sonnet", status: "running",
  agent: "code-reviewer", task: "Review the diff for correctness",
  cwd: "/tmp/project/dev/interact", project: "interact", pid: 4242,
  cost_usd: 1.25, input_tokens: 120000, output_tokens: 8000,
} as any;

test("the system prompt is reachable as a link", () => {
  const html = chatDocument({ nonce: "n", name: "code-reviewer", status: "running", turns: [],
    run: RUN, files: [{ label: "system prompt", path: "/tmp/project/.claude/agents/code-reviewer.md" }] });
  assert.match(html, /system prompt/);
  assert.match(html, /code-reviewer\.md/);
});

test("the brief it was given is shown, not just its name", () => {
  const html = chatDocument({ nonce: "n", name: "code-reviewer", status: "running", turns: [], run: RUN });
  assert.match(html, /Review the diff for correctness/);
});

test("context size is shown as tokens, not only as cost", () => {
  const html = chatDocument({ nonce: "n", name: "code-reviewer", status: "running", turns: [], run: RUN });
  assert.match(html, /120,000|120k/i);
});

test("identity a person can USE is there; machine identity is not", () => {
  /* The professional sweep measured the about-block as half plumbing — a dead pid, a full UUID
     nobody can act on. Provider and model stay (a person chooses by them); the process number and
     session id live in the registry, where machines look. */
  const html = chatDocument({ nonce: "n", name: "code-reviewer", status: "running", turns: [], run: RUN });
  for (const fact of ["claude", "sonnet"]) {
    assert.ok(html.includes(fact), `missing ${fact}`);
  }
  assert.ok(!html.includes("4242"), "a pid is plumbing, not information");
  assert.ok(!html.includes("abcd1234"), "so is a session id");
});

test("a run with no definition does not render an empty system-prompt row", () => {
  const html = chatDocument({ nonce: "n", name: "claude", status: "done", turns: [],
    run: { ...RUN, agent: null }, files: [] });
  assert.ok(!/system prompt/.test(html));
});

test("everything in the details is escaped — a task is user text", () => {
  const html = chatDocument({ nonce: "n", name: "x", status: "done", turns: [],
    run: { ...RUN, task: "<img src=x onerror=alert(1)>" } });
  assert.ok(!html.includes("<img src=x"));
});

test("a file's PATH is shown, not hidden behind a label", () => {
  // "system prompt (a file link is enough)" — the path IS the deliverable. A chip that only says
  // "system prompt" tells you a file exists somewhere; it does not tell you where.
  const html = chatDocument({ nonce: "n", name: "g", status: "done", turns: [], run: RUN,
    files: [{ label: "system prompt", path: "/tmp/project/.claude/agents/g.md" }] });
  assert.match(html, /\/tmp\/project\/\.claude\/agents\/g\.md/);
});

// Watching a response arrive means the view UPDATES, not reloads. Replacing the whole document on
// every registry write threw away scroll position and whatever was half-typed in the composer.

test("the transcript can be rendered on its own, for patching in", () => {
  const html = transcriptFragment({
    turns: [{ kind: "text", text: "hello" }], name: "r", awaitingReply: false,
  });
  assert.match(html, /hello/);
  assert.ok(!/<html|<body|<script/.test(html), "a fragment, not a document");
});

test("an unsupported external provider run keeps its history without offering a send route", () => {
  const html = chatDocument({
    nonce: "n", commands: [], turns: [{ kind: "text", text: "existing external history" }],
    name: "Claude session", status: "foreign", readOnly: true,
    run: { ...RUN, provider: "claude", kind: "provider_root", run_id: "external-1" },
  });
  assert.match(html, /existing external history/);
  assert.match(html, /read.only|own session|cannot send/i);
  assert.doesNotMatch(html, /data-route-id=.*claude/i,
    "detecting an external provider must not invent a supported conversation route");
});

test("the Conversation gear opens the Interact settings workspace", () => {
  const rendered = chatDocument({ nonce: "n", commands: [], turns: [], name: "agent",
    status: "waiting", run: RUN });
  assert.match(rendered, /id="openSettings"[^>]*>⚙/);
  assert.match(rendered, /command: "interact\.openDashboard"/);
});

test("the answer stays primary while agent activity is a truthful collapsed disclosure", () => {
  const rendered = chatDocument({ nonce: "n", commands: [], turns: [{ kind: "text", text: "visible answer" }],
    name: "agent", status: "waiting", run: RUN, activity: [{ run: RUN, current_tool: null,
      transcript: [], children: [], omitted_children: 0 }] });
  assert.ok(rendered.indexOf("visible answer") < rendered.indexOf("Agent activity"));
  assert.doesNotMatch(rendered, /No transcript reported yet/);
  assert.match(rendered, /<details class="details"[^>]*><summary>about this agent<\/summary>/);
  assert.doesNotMatch(rendered, /<details class="details"[^>]* open/);
  assert.match(rendered, /<details class="activity-run"[^>]*>/);
  assert.doesNotMatch(rendered, /<details class="activity-run"[^>]* open/);
  assert.match(rendered, /<summary><span>[^<]+<\/span><b>[^<]+<\/b><\/summary>/);
});

test("the fragment carries the answering state too", () => {
  const html = transcriptFragment({ turns: [], name: "r", awaitingReply: true });
  assert.match(html, /answering/i);
});

test("a sub-agent says who sent it — that is half of 'its own context'", () => {
  const html = chatDocument({
    nonce: "n", name: "researcher", status: "running", turns: [],
    run: { ...RUN, name: "researcher", agent: "researcher" } as any,
    sentBy: "reviewer",
  });
  assert.match(html, /sent by/i);
  assert.match(html, /reviewer/);
});

test("a top-level run does not claim a sender it never had", () => {
  const html = chatDocument({ nonce: "n", name: "r", status: "running", turns: [], run: RUN });
  assert.ok(!/sent by/i.test(html));
});

test("only the bounded approval title may use emergency one-character wrapping", () => {
  // `overflow-wrap: anywhere` lets a grid track's min-content width fall to 1ch, so the instant
  // the sidebar narrowed — which is exactly what clicking a file link does — paths and session
  // ids wrapped one glyph per line. `break-word` breaks the same values without lying to the
  // layout about how narrow the column may be.
  const html = chatDocument({ nonce: "n", name: "g", status: "done", turns: [], run: RUN });
  const withoutApprovalTitleRule = html.replace(
    /\.approval > h3\s*\{[^}]*overflow-wrap:\s*anywhere;?[^}]*\}/,
    "",
  );
  assert.equal(/overflow-wrap:\s*anywhere/.test(withoutApprovalTitleRule), false);
  assert.match(html, /\.approval\s*\{[^}]*min-width:\s*0/);
  assert.match(html, /\.approval > h3\s*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(html, /overflow-wrap:\s*break-word/);
});

test("approvals and structured input are distinct accessible requests without raw method titles", () => {
  const interaction = (runId: string, kind: string, id: string, title: string, fields: unknown[]) => ({
    run_id: runId, event: { kind: "interaction",
      text: "The provider is waiting for your decision.", interaction: {
      id, kind, title, fields, disclosure: ["bounded detail"],
    } },
  });
  const html = chatDocument({ nonce: "n", name: "g", status: "waiting", turns: [], run: RUN,
    console: { phase: "ready", approvals: [
      interaction("run-a", "command_approval", "shared", "item/commandExecution/requestApproval", [
        { key: "decision", kind: "choice", label: "Allow?", required: true,
          options: ["accept", "decline"] },
      ]),
      interaction("run-b", "user_input", "shared", "item/tool/requestUserInput", [
        { key: "choice", kind: "choice", label: "Choose", required: true, options: ["A", "B"] },
        { key: "notes", kind: "text", label: "Explain", required: true, options: [] },
      ]),
    ] as never } });
  assert.match(html, /class="approval[^\"]*approval-command/);
  assert.match(html, /class="approval[^\"]*structured-input/);
  assert.match(html, /aria-labelledby="[^"]+"/g);
  const headings = [...html.matchAll(/<h3 id="([^"]+)"/g)].map((match) => match[1]);
  const labels = [...html.matchAll(/aria-labelledby="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(headings).size, 2, "run-scoped interactions must not duplicate DOM ids");
  assert.deepEqual(labels, headings, "each request labels itself with its own local heading");
  assert.match(html, /<fieldset[^>]*>.*<legend/s, "choice groups retain their accessible legend");
  assert.ok(!html.includes("item/commandExecution/requestApproval"));
  assert.ok(!html.includes("item/tool/requestUserInput"));
  assert.ok(!html.includes("The provider is waiting for your decision."),
    "the card purpose already says this; repeated filler is not content");
  assert.match(html, />Approve command</);
  assert.match(html, />Send details</);
  const approvalRule = /\.approval\s*\{([^}]*)\}/.exec(html)?.[1] ?? "";
  assert.ok(!approvalRule.includes("inputValidation-warning"),
    "ordinary interaction cards use restrained VS Code surfaces, not a mustard warning slab");
});

test("the details panel remembers whether it was open", () => {
  // It holds session id, cwd and the system-prompt path — what a debugging user opens first —
  // and the document is rebuilt on every agent switch, so it collapsed again each time.
  const html = chatDocument({ nonce: "n", name: "g", status: "done", turns: [], run: RUN });
  assert.match(html, /vscode\.getState\(\)/);
  assert.match(html, /detailsOpen/);
  assert.match(html, /addEventListener\("toggle"/);
});

test("the slash menu actually has entries when commands are supplied", () => {
  // The defect: the one production caller never passed `commands`, so <ul id="palette"> rendered
  // with zero <li> in every state — the "/" button was enabled and opened nothing. Both sides
  // passed their own unit tests; only the connection was missing, and `noUnusedLocals` cannot see
  // that shape because CHAT_COMMANDS *is* imported and *is* used, just for validation instead.
  const doc = chatDocument({
    nonce: "n", turns: [], name: "visual-critic",
    commands: [
      { slash: "/team", title: "Open the team", detail: "the workplace", command: "interact.agents.team", needsAgent: false },
      { slash: "/stop", title: "Stop", detail: "interrupt it", command: "interact.agents.stop", needsAgent: true },
    ],
  } as never);
  const items = doc.match(/<li[^>]*data-command=/g) ?? [];
  assert.equal(items.length, 2, "the palette rendered no entries");
  assert.ok(doc.includes("/team"));
});

test("a command needing an agent is marked when none is selected", () => {
  const withNoAgent = chatDocument({
    nonce: "n", turns: [],
    commands: [{ slash: "/stop", title: "Stop", detail: "x", command: "interact.agents.stop", needsAgent: true }],
  } as never);
  assert.match(withNoAgent, /data-needs-agent="1"/,
    "greying it out is the honest answer; hiding the whole menu was not");
});

test("the header answers the questions, without a trip to the top", () => {
  /* "When i click on a conversation, i have to go to the top to view info... instead things should
     be accessible in the header." The identity facts — which model, what it cost, its state — live
     in the header itself, which stays put while the transcript scrolls. */
  const html = chatDocument({ nonce: "n", name: "researcher", status: "running", turns: [],
    run: { ...RUN, model: "ollama/deepseek-v4-pro:cloud", cost_usd: 0.42 } });
  const header = html.slice(html.indexOf("<header"), html.indexOf("</header>"));
  assert.ok(header.includes("deepseek-v4-pro:cloud"), "the model belongs in the header");
  assert.ok(header.includes("0.42"), "so does what it has cost");
  assert.match(html, /header\s*{[^}]*position:\s*sticky/, "and the header must not scroll away");
});

test("the header is drawn in the theme's own colours, not an inverted plate", () => {
  /* "in the conversation header the contrast is really badly adapted to themes." The plate painted
     its letters in the PANEL BACKGROUND colour — a light plate carrying light letters the moment
     the theme changes. Foreground on the sidebar's own tokens adapts by construction. */
  const html = chatDocument({ nonce: "n", name: "g", status: "done", turns: [], run: RUN });
  const css = html.slice(html.indexOf("header {"), html.indexOf("header {") + 500);
  assert.ok(!css.includes("color: var(--wp-bg)"),
    "background-coloured letters is the defect he is describing");
});

test("one of your own sessions reads in full but cannot pretend to send", () => {
  const doc = chatDocument({
    nonce: "n", commands: [], turns: [], name: "interact", status: "foreign",
    readOnly: true, awaitingReply: false, run: undefined as never, files: [], sentBy: null,
  } as never);
  assert.match(doc, /watch here, reply in its window/,
    "the composer must say why it is quiet, not just grey out");
  assert.match(doc, /<textarea[^>]*disabled/);
  assert.match(doc, /<button type="submit" disabled/);
});
