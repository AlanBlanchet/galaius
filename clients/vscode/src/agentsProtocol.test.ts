/** Generated agent records survive the extension's tolerant disk reader without projection. */
import { strict as assert } from "node:assert";
import { createRequire } from "node:module";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const require_ = createRequire(import.meta.url);
const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out", "agents.js");

test("generated run and event fields cross the historical registry reader intact", (t) => {
  // The old event reader projected nine legacy fields and silently discarded every causal,
  // approval and provider-cursor field even though Python had already normalized them.
  if (!fs.existsSync(built)) return t.skip("run npm run compile first");
  const root = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)), "..", "..", "out", "tests",
    `agents-protocol-${process.pid}-${Date.now()}`,
  );
  const directory = path.join(root, ".interact", "out", "agents");
  fs.mkdirSync(directory, { recursive: true });
  const previousHome = process.env.HOME;
  process.env.HOME = root;
  const run = {
    run_id: "root-1", kind: "conversation", provider: "provider-a", name: "root",
    status: "done", root_run_id: "root-1", spawned_by_event_id: null,
    provider_session_id: "provider-session", provider_turn_id: "provider-turn",
    connection: "local_session", requested_model: "example", requested_criterion: null,
    cataloged_at: 1787875200, charge_path: "subscription_quota", cost_certainty: "unknown",
    capabilities: ["streaming", "resume", "cancel", "approvals", "collaboration"],
    tools: ["read"],
  };
  const event = {
    kind: "interaction", text: "interaction requested", event_id: "event-1", sequence: 7,
    provider_cursor: "cursor-1", parent_event_id: "parent-event", turn_id: "turn-1",
    agent_run_id: "root-1", status: "interaction_required",
    interaction: {
      id: "interaction-1", kind: "command_approval", title: "Allow write?",
      fields: [{ key: "decision", label: "Decision", kind: "choice", required: true,
        options: ["decline", "acceptForSession"] }],
    },
    tool: null, tool_input: "", tool_id: "", at: 1787875201,
  };
  fs.writeFileSync(path.join(directory, "root-1.json"), JSON.stringify(run));
  fs.writeFileSync(path.join(directory, "root-1.jsonl"), `${JSON.stringify(event)}\n`);
  fs.writeFileSync(path.join(directory, "malicious.json"), JSON.stringify({
    ...run,
    run_id: "../../outside",
    status: "running",
  }));
  try {
    delete require_.cache[require_.resolve(built)];
    const reader = require_(built);
    assert.deepEqual(reader.readAgentRuns(), [run],
      "an unsafe persisted id must be rejected before stream liveness resolves an event path");
    assert.deepEqual(reader.readAgentActivity("root-1", 10)[0], event,
      "the reader may add display defaults but may not project away canonical event fields");
    assert.deepEqual(reader.readAgentActivity("../../outside", 10), []);
    assert.equal(reader.rawEventsPath("../../outside"), null,
      "public path helpers must fail closed rather than joining outside the registry");
  } finally {
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    fs.rmSync(root, { recursive: true });
  }
});

test("activityOf uses bounded history and incremental suffix reads at 100k events", (t) => {
  // Spy on the production entrypoint: a fast unused helper cannot satisfy this contract.
  if (!fs.existsSync(built)) return t.skip("run npm run compile first");
  delete require_.cache[require_.resolve(built)];
  const reader = require_(built);
  const root = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)), "..", "..", "out", "tests",
    `agents-scale-${process.pid}-${Date.now()}`,
  );
  const directory = path.join(root, ".interact", "out", "agents");
  fs.mkdirSync(directory, { recursive: true });
  const previousHome = process.env.HOME;
  process.env.HOME = root;
  const events = Array.from({ length: 100_000 }, (_, index) =>
    JSON.stringify({ kind: "text", event_id: `event-${index}`, text: `line-${index}` }));
  fs.writeFileSync(path.join(directory, "root.jsonl"), `${events.join("\n")}\n`);
  try {
    const mutableFs = require_("node:fs") as typeof fs;
    const originalRead = mutableFs.readFileSync;
    let bytesRead = 0;
    mutableFs.readFileSync = ((...args: Parameters<typeof fs.readFileSync>) => {
      const value = originalRead(...args as [fs.PathOrFileDescriptor, BufferEncoding]);
      bytesRead += typeof value === "string" ? Buffer.byteLength(value) : value.byteLength;
      return value;
    }) as typeof fs.readFileSync;
    const run = { run_id: "root", provider: "synthetic", name: "root", status: "done" };
    const first = reader.activityOf(run, 40);
    const initialBytes = bytesRead;
    fs.appendFileSync(path.join(directory, "root.jsonl"),
      `${JSON.stringify({ kind: "text", event_id: "event-100000", text: "new" })}\n`);
    bytesRead = 0;
    const second = reader.activityOf(run, 40);
    mutableFs.readFileSync = originalRead;
    assert.equal(first.length, 40);
    assert.equal(second.at(-1).text, "new");
    assert.ok(initialBytes < 128_000, "initial activityOf reads one bounded reverse tail");
    assert.ok(bytesRead < 4_096, "the next activityOf reads only the appended suffix");
  } finally {
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    fs.rmSync(root, { recursive: true });
  }
});

for (const mode of ["torn", "rotation", "crash-replay"] as const) test(
  `stateful activity reader handles ${mode} independently`, (t) => {
  if (!fs.existsSync(built)) return t.skip("run npm run compile first");
  delete require_.cache[require_.resolve(built)];
  const reader = require_(built);
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "out", "tests",
    `agents-durability-${process.pid}-${Date.now()}`);
  const directory = path.join(root, ".interact", "out", "agents");
  fs.mkdirSync(directory, { recursive: true });
  const previousHome = process.env.HOME;
  process.env.HOME = root;
  const file = path.join(directory, "root.jsonl");
  try {
    fs.writeFileSync(file, `${JSON.stringify({ kind: "text", event_id: "base", text: "base" })}\n`);
    const activity = reader.createAgentActivityReader({ eventLimit: 40, transcriptLimit: 300 });
    activity.read("root");
    if (mode === "torn") {
      fs.appendFileSync(file, '{"kind":"text","event_id":"torn","text":"incomplete"');
      assert.equal(activity.read("root").events.some(
        (event: { event_id?: string }) => event.event_id === "torn"), false);
      fs.appendFileSync(file, '}\n');
      assert.equal(activity.read("root").events.filter(
        (event: { event_id?: string }) => event.event_id === "torn").length, 1,
      "a repaired torn line is emitted exactly once");
    } else {
      const replacement = path.join(directory, "replacement.jsonl");
      fs.writeFileSync(replacement,
        `${JSON.stringify({ kind: "done", event_id: "terminal", status: "completed" })}\n`);
      fs.renameSync(replacement, file);
      const rotated = activity.read("root");
      if (mode === "rotation") {
        assert.deepEqual(rotated.events.map(
          (event: { event_id?: string }) => event.event_id), ["terminal"],
        "rotation resets the prior cursor and projection");
      } else {
        const replay = reader.createAgentActivityReader({ eventLimit: 40, transcriptLimit: 300 });
        assert.equal(replay.read("root").events.filter(
          (event: { event_id?: string }) => event.event_id === "terminal").length, 1,
        "crash replay keeps one terminal record without regression");
      }
    }
  } finally {
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    fs.rmSync(root, { recursive: true });
  }
  },
);

test("generated runtime decoders reject malformed nested wire fields", (t) => {
  // Declarations are erased at runtime; this proves Python generation also owns hostile JSON.
  const generated = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)), "..", "out", "generated", "types.js",
  );
  if (!fs.existsSync(generated)) return t.skip("run npm run compile first");
  delete require_.cache[require_.resolve(generated)];
  const wire = require_(generated);
  assert.equal(typeof wire.decodeConversationCommand, "function");
  assert.equal(typeof wire.decodeConversationResponse, "function");
  assert.equal(typeof wire.decodeConversationStreamEvent, "function");
  assert.equal(typeof wire.decodeAgentRun, "function");
  assert.equal(typeof wire.decodeAgentEvent, "function");
  const start = {
    version: 1, request_id: "start", method: "start",
    request: {
      route_id: "session", prompt: "hello", selection: { model: "example" },
      workspace_root: "/synthetic",
    },
  };
  assert.deepEqual(wire.decodeConversationCommand(start), start);
  for (const malformed of [
    { ...start, method: "future" },
    { ...start, request: { ...start.request, selection: { model: 7 } } },
    { ...start, request: { ...start.request, route_id: null } },
    { ...start, request_id: 7 },
  ]) {
    assert.throws(() => wire.decodeConversationCommand(malformed));
  }
  const interaction = {
    version: 1, request_id: "interaction", method: "interaction", run_id: "root",
    submission: { interaction_id: "interaction-1", values: { confirmed: false } },
  };
  assert.deepEqual(wire.decodeConversationCommand(interaction), interaction,
    "a false response is present even though it is falsy");
  const emptyInteraction = {
    ...interaction, submission: { ...interaction.submission, values: {} },
  };
  assert.deepEqual(wire.decodeConversationCommand(emptyInteraction), emptyInteraction,
    "the structural decoder allows an empty all-optional response; the pending schema owns completeness");
  const response = {
    version: 1, type: "response", request_id: "catalog", method: "catalog", ok: true,
    catalog: {
      version: 1, cataloged_at: 1787875200, criteria: [], routes: [{
        id: "session", provider: "provider-a", connection: "local_session", label: "Session",
        availability: "available", reason: "", charge_path: "subscription_quota",
        cost_certainty: "unknown", billing_note: "Account impact unknown.",
        models: [], cataloged_at: 1787875200,
      }],
    },
  };
  assert.deepEqual(wire.decodeConversationResponse(response), response);
  assert.throws(() => wire.decodeConversationResponse({
    ...response, catalog: { ...response.catalog, routes: [{ ...response.catalog.routes[0], connection: "future" }] },
  }));
  const stream = {
    version: 1, type: "event",
    run: { run_id: "root", provider: "provider-a", name: "root", status: "running" },
    event: { kind: "interaction", status: "interaction_required", interaction: {
      id: "interaction-1", kind: "user_input", title: "Choose",
      fields: [{ key: "answer", kind: "choice", label: "Answer", options: ["yes", "no"] }],
    } },
  };
  assert.deepEqual(wire.decodeConversationStreamEvent(stream), stream);
  assert.throws(() => wire.decodeConversationStreamEvent({
    ...stream, event: { ...stream.event, interaction: {
      ...stream.event.interaction, fields: [{ ...stream.event.interaction.fields[0], kind: "future" }],
    } },
  }));
  const persistedRun = {
    run_id: "root", provider: "provider-a", name: "root", kind: "conversation",
    status: "done", charge_path: "subscription_quota", cost_certainty: "unknown",
    capabilities: ["streaming"], tools: [], started_at: 1, foreign: false, last: "done",
  };
  assert.deepEqual(wire.decodeAgentRun(persistedRun), persistedRun);
  assert.throws(() => wire.decodeAgentRun({ ...persistedRun, capabilities: ["future"] }));
  assert.throws(() => wire.decodeAgentRun({ ...persistedRun, charge_path: { nested: true } }));
  const persistedEvent = {
    kind: "interaction", status: "interaction_required", interaction: {
      id: "interaction-1", kind: "user_input", title: "Choose",
      fields: [{ key: "answer", kind: "choice", label: "Answer", options: ["yes", "no"] }],
    },
  };
  assert.deepEqual(wire.decodeAgentEvent(persistedEvent), persistedEvent);
  assert.throws(() => wire.decodeAgentEvent({ ...persistedEvent, kind: "future" }));
  assert.throws(() => wire.decodeAgentEvent({
    ...persistedEvent,
    interaction: { ...persistedEvent.interaction, fields: [{ key: 7 }] },
  }));
});
