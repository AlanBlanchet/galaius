/** Process-bound acceptance for the extension-owned newline-JSON conversation client. */
import { strict as assert } from "node:assert";
import { createRequire } from "node:module";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const require_ = createRequire(import.meta.url);
const {
  CONVERSATION_SHUTDOWN_GRACE_MS,
  createConversationClient,
  usesConversationTransport,
} = require_("../out/conversationClient.js");

const fakeHost = path.join(
  path.dirname(fileURLToPath(import.meta.url)), "..", "test", "fixtures", "conversationFakeHost.ts",
);

function fake(mode = "healthy") {
  return {
    command: process.execPath,
    args: ["--experimental-strip-types", fakeHost],
    cwd: path.dirname(fakeHost),
    env: { ...process.env, INTERACT_FAKE_CONVERSATION_MODE: mode },
  };
}

async function waitForState(client: { state(): string }, expected: string): Promise<void> {
  const deadline = Date.now() + 1_000;
  while (client.state() !== expected && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

test("only conversation runs use the stateful transport", () => {
  // The extension has two delivery contracts: historical process runs retain the established CLI
  // send path, while provider conversations stay on their one typed host process.
  for (const [kind, expected] of [
    [undefined, false],
    ["process", false],
    ["conversation", true],
    ["provider_child", false],
    ["provider_root", false],
  ] as const) {
    assert.equal(usesConversationTransport({
      run_id: "run",
      provider: "provider-a",
      name: "agent",
      kind,
    }), expected, String(kind));
  }
});

test("one stdio process negotiates, catalogs, starts, streams, resumes, interacts and cancels", async () => {
  // No unit mock covers framing, process lifetime and an event interleaved with responses.
  const streamed: string[] = [];
  const deliveryOrder: string[] = [];
  const states: string[] = [];
  let received: (() => void) | undefined;
  const streamedEvent = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("event timeout")), 1000);
    received = () => { clearTimeout(timer); resolve(); };
  });
  const client = createConversationClient(fake(), {
    onState: (state) => states.push(state),
    onEvent: (_run, event) => {
      deliveryOrder.push(`event:${event.kind}`);
      streamed.push(event.text);
      if (streamed.length === 2) received?.();
    },
  });
  try {
    const catalog = await client.catalog();
    assert.equal(catalog.routes[0].connection, "local_session");
    assert.equal(catalog.routes[0].charge_path, "subscription_quota");

    const started = await client.start({
      route_id: "session-route",
      prompt: "Inspect the workspace",
      selection: { model: "example-model" },
      workspace_root: path.dirname(fakeHost),
    });
    deliveryOrder.push("start-continuation");
    assert.equal(started.run_id, "root-run");
    await streamedEvent;
    assert.equal(deliveryOrder[0], "start-continuation",
      "the consumer must install the returned run before any same-chunk event callback observes it");
    assert.deepEqual(streamed, [
      "streamed café answer",
      "child event with the same provider-local id",
    ], "UTF-8 framing survives split bytes; dedupe is scoped by run id plus event id");

    assert.equal((await client.send("root-run", "continue")).run_id, "root-run");
    assert.equal((await client.interact("root-run", {
      interaction_id: "interaction-1", values: { decision: "decline" },
    })).run_id, "root-run");
    assert.equal((await client.cancel("root-run")).status, "stopped");
    assert.equal(client.state(), "ready");
    assert.deepEqual(states, ["connecting", "ready"], "each client transition is emitted once");

    // A response and its first event may occupy one stdout chunk. This stays in the process test
    // because only byte framing plus Promise scheduling can reproduce the consumer-order race.
    const sameChunkOrder: string[] = [];
    let terminalReceived: (() => void) | undefined;
    const terminalEvent = new Promise<void>((resolve) => { terminalReceived = resolve; });
    const sameChunk = createConversationClient(fake("same_chunk_terminal"), {
      onEvent: () => { sameChunkOrder.push("event"); terminalReceived?.(); },
    });
    try {
      await sameChunk.catalog();
      await sameChunk.start({
        route_id: "session-route",
        prompt: "complete immediately",
        selection: { model: "example-model" },
        workspace_root: path.dirname(fakeHost),
      });
      sameChunkOrder.push("start-continuation");
      await terminalEvent;
      assert.deepEqual(sameChunkOrder, ["start-continuation", "event"],
        "the returned run is installed before a same-chunk terminal event reaches the view");
    } finally {
      sameChunk.dispose();
    }
  } finally {
    client.dispose();
  }
});

test("dispose and protocol failure close stdin long enough for host cleanup", async () => {
  // The Python host owns provider subprocesses. Abruptly killing it on webview shutdown bypasses
  // its EOF cleanup and can orphan the provider session after the extension window is gone.
  assert.ok(CONVERSATION_SHUTDOWN_GRACE_MS >= 4_000,
    "the extension owner outlives the host's two-second graceful and one-second terminate bounds");
  const artifactDir = path.resolve(path.dirname(fakeHost), "..", "..", "..", "out", "tests", "conversation-client");
  fs.mkdirSync(artifactDir, { recursive: true });
  for (const mode of ["cleanup", "malformed_cleanup"] as const) {
    const marker = path.join(artifactDir, `cleanup-${mode}-${process.pid}-${Date.now()}.txt`);
    const spec = fake(mode);
    spec.env.INTERACT_FAKE_CLEANUP_MARKER = marker;
    const client = createConversationClient(spec);
    try {
      await client.catalog();
      if (mode === "cleanup") client.dispose();
      else {
        const deadline = Date.now() + 1000;
        while (client.state() !== "crashed" && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        assert.equal(client.state(), "crashed");
      }
      const deadline = Date.now() + 1000;
      while (!fs.existsSync(marker) && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      assert.equal(fs.readFileSync(marker, "utf8"), "stdin closed", mode);
    } finally {
      client.dispose();
      if (fs.existsSync(marker)) fs.unlinkSync(marker);
    }
  }
});

test("provider errors cannot echo paths, prompts or credential-shaped text", async () => {
  // The host response is hostile even though it is local: provider stderr routinely carries
  // account and filesystem context, neither of which belongs in the panel or extension log.
  const visible: string[] = [];
  const client = createConversationClient(fake("adversarial_error"), {
    onError: (message) => visible.push(message),
  });
  try {
    let rejected = "";
    try {
      await client.catalog();
      assert.fail("the hostile provider response must fail initialization");
    } catch (error) {
      rejected = error instanceof Error ? error.message : String(error);
    }
    assert.match(rejected, /internal_error: The local conversation host failed/i);
    assert.equal(visible.length, 1, "the same generic failure must reach the visible host callback");
    const shown = `${visible.join(" ")} ${rejected}`;
    for (const secret of ["api_key", "secret-value", "/home/private", "private prompt"]) {
      assert.ok(!shown.includes(secret), `${secret} must stay behind the process boundary`);
    }
  } finally {
    client.dispose();
  }
});

test("incompatible, crashed and malformed hosts fail visibly without reconnecting", async () => {
  // These are distinct external-seam failures but share one invariant: the same client becomes
  // terminal and a second paid/ambiguous process is never started automatically.
  for (const mode of ["incompatible", "crash", "crash_on_start", "malformed"] as const) {
    const errors: string[] = [];
    const states: string[] = [];
    const client = createConversationClient(fake(mode), {
      onError: (message) => errors.push(message),
      onState: (state) => states.push(state),
    });
    try {
      if (mode === "incompatible") {
        await assert.rejects(client.catalog(), /incompatible/i);
      } else if (mode === "crash_on_start") {
        await client.catalog();
        await assert.rejects(client.start({
          route_id: "session-route",
          prompt: "retain this exact prompt",
          selection: { model: "example-model" },
          workspace_root: path.dirname(fakeHost),
        }), /stopped|crash|protocol|closed/i);
        assert.equal(client.state(), "crashed", "the first dispatch must expose the bridge exit");
      } else {
        await client.catalog();
        await waitForState(client, "crashed");
        assert.equal(client.state(), "crashed", `${mode} must make the one process terminal`);
        await assert.rejects(client.send("root-run", "do not retry"),
          /stopped|crash|protocol|malformed/i);
      }
      assert.ok(errors.length > 0, `${mode} must be visible to the host UI`);
      assert.notEqual(client.state(), "connecting");
      for (const state of new Set(states)) {
        assert.equal(states.filter((candidate) => candidate === state).length, 1,
          `${mode} must emit ${state} exactly once`);
      }
    } finally {
      client.dispose();
    }
  }
});

test("pre-initialize launch failures are distinct, safe, terminal, and explicitly reloadable", async (context) => {
  const cases = [
    {
      name: "exit 1 with private stderr",
      spec: fake("exit_before_initialize"),
      expected: /exited before initialization \(exit 1; 140 stderr bytes\).*reload/i,
    },
    {
      name: "missing executable",
      spec: { command: path.join(path.dirname(fakeHost), "absent-interact"), args: [], cwd: path.dirname(fakeHost) },
      expected: /was not found.*Install it.*reload/i,
    },
    { name: "incompatible methods", spec: fake("incompatible"), expected: /incompatible.*reload/i },
  ] as const;
  for (const scenario of cases) await context.test(scenario.name, async () => {
    const visible: string[] = [];
    const states: string[] = [];
    const client = createConversationClient(scenario.spec, {
      onError: (message) => visible.push(message), onState: (state) => states.push(state),
    });
    try {
      await assert.rejects(client.catalog(), scenario.expected);
      assert.equal(client.state(), "crashed");
      await assert.rejects(client.catalog(), scenario.expected, "a terminal client must not retry");
      assert.equal(states.filter((state) => state === "connecting").length, 1);
      assert.equal(visible.length, 1);
      assert.doesNotMatch(visible[0], /private bridge diagnostic|!{3}/,
        "stderr content must never cross into the UI");
    } finally {
      client.dispose();
      assert.equal(client.state(), "closed", "dispose must leave the failed bridge terminal");
    }
  });
});

test("a bridge that dies at startup logs WHAT it said, without showing it", () => {
  /* Live cold entry gave "(exit 1; 638 stderr bytes)" — 638 bytes of diagnosis counted and thrown
     away. That text turned out to name a real version skew (`Unknown command "console"`), so it
     must be captured. But the neighbouring contract is deliberate and right: a child's stderr is
     untrusted and may carry paths or secrets, so it never crosses into the UI. The tail therefore
     goes to `onDiagnostic` (the extension's log) while `onError` keeps the count. */
  const src = fs.readFileSync(new URL("./conversationClient.ts", import.meta.url), "utf8");
  assert.match(src, /stderrTail/, "the text is kept, not only its length");
  assert.match(src, /onDiagnostic/, "and routed to the log");
  const exitMessage = src.slice(src.indexOf("exited before initialization"), src.indexOf("exited before initialization") + 160);
  assert.doesNotMatch(exitMessage, /stderrTail|said\(/, "the USER-facing message carries no stderr");
  assert.match(exitMessage, /stderrBytes/, "it still says how much there was to look at");
});

test("a missing program is named, and the diagnostic actually reaches a listener", () => {
  /* Two round-48 findings in one place.

     ENOENT said "Interact is not installed" when the missing program was `uv` — the spawn uses
     `uv run --directory` for a project checkout. The user got confident, wrong advice next to a
     RELOAD BRIDGE button, which compounds with any other fault in that path.

     And `onDiagnostic` was declared, computed and bounded — then never wired to anything, so the
     stderr the whole change existed to preserve was dropped on the floor. Producing without a
     consumer is not delivering. */
  const src = fs.readFileSync(new URL("./conversationClient.ts", import.meta.url), "utf8");
  const at = src.indexOf('=== "ENOENT"');
  const enoent = src.slice(at, src.indexOf('conversation bridge could not start', at));
  assert.match(enoent, /spec\.command/, "the message names the program that is actually missing");

  const view = fs.readFileSync(new URL("./chatView.ts", import.meta.url), "utf8");
  assert.match(view, /onDiagnostic/, "and something must actually listen for the diagnostic");
});
