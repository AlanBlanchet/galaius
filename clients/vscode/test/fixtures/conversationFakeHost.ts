#!/usr/bin/env -S node --experimental-strip-types --disable-warning=MODULE_TYPELESS_PACKAGE_JSON
/** Deterministic newline-JSON host used by conversation integration tests.
 *
 * It deliberately has no imports from the product client: crossing a real child-process/stdin
 * boundary is the integration evidence, and sharing its parser would make both sides fail alike.
 */
import * as readline from "node:readline";
import * as fs from "node:fs";

/** @typedef {"healthy" | "exit_before_initialize" | "controlled_post_catalog_exit" |
 * "adversarial_error" | "incompatible" | "api_catalog" | "crash" | "malformed" |
 * "malformed_cleanup" | "crash_on_start" | "same_chunk_terminal" | "cleanup"} Mode */
const rawMode = process.env.INTERACT_FAKE_CONVERSATION_MODE ?? "healthy";
/** @type {readonly Mode[]} */
const MODES = ["healthy", "exit_before_initialize", "controlled_post_catalog_exit",
  "adversarial_error", "incompatible", "api_catalog", "crash", "malformed", "malformed_cleanup",
  "crash_on_start", "same_chunk_terminal", "cleanup"];
if (!MODES.includes(rawMode)) throw new Error("unknown fake conversation mode");
const mode = rawMode;
const commandLog = process.env.INTERACT_FAKE_CONVERSATION_LOG;
const launchLog = process.env.INTERACT_FAKE_LAUNCH_LOG;
const readyMarker = process.env.INTERACT_FAKE_READY_MARKER;
const releaseMarker = process.env.INTERACT_FAKE_RELEASE_MARKER;

if (process.argv.includes("--version")) {
  if (process.env.INTERACT_FAKE_VERSION_MODE === "malformed") {
    process.stdout.write("interact development\n");
    process.exit(0);
  }
  if (process.env.INTERACT_FAKE_VERSION_MODE === "failure") process.exit(1);
  if (process.env.INTERACT_FAKE_VERSION_MODE === "timeout") {
    setTimeout(() => process.exit(0), 4_000);
  } else {
    const versionPath = `${process.argv[1]}.version`;
    let version = "0.39.0";
    if (fs.existsSync(versionPath)) {
      const metadata = fs.lstatSync(versionPath);
      if (!metadata.isFile() || metadata.size < 1 || metadata.size > 32) process.exit(1);
      const configured = fs.readFileSync(versionPath, "utf8").trim();
      if (!/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/.test(configured)) process.exit(1);
      version = configured;
    }
    process.stdout.write(`interact ${version}\n`);
    process.exit(0);
  }
}
if (launchLog) fs.appendFileSync(launchLog, "launch\n");
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

if (mode === "exit_before_initialize") {
  process.stderr.write("private bridge diagnostic".padEnd(140, "!"));
  process.exit(1);
}

/** @param {object} value */
// @ts-expect-error TS7006: the fixture stays JS syntax because tests copy it to an extensionless executable.
function write(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

/** @param {object} value @param {string} character */
// @ts-expect-error TS7006: the fixture stays JS syntax because tests copy it to an extensionless executable.
function writeAcrossUtf8Boundary(value, character) {
  const line = Buffer.from(`${JSON.stringify(value)}\n`);
  const marker = Buffer.from(character);
  const offset = line.indexOf(marker);
  if (offset < 0 || marker.length < 2) throw new Error("split marker must be multibyte and present");
  process.stdout.write(line.subarray(0, offset + 1));
  setImmediate(() => process.stdout.write(line.subarray(offset + 1)));
}

const run = {
  run_id: "root-run",
  provider: "provider-a",
  name: "root",
  task: "Inspect the workspace",
  status: "running",
  kind: "conversation",
  root_run_id: "root-run",
  parent_run_id: null,
  connection: "local_session",
  model: "example-model",
  requested_model: "example-model",
  requested_criterion: null,
  charge_path: "subscription_quota",
  cost_certainty: "unknown",
  capabilities: ["streaming", "resume", "cancel", "approvals", "collaboration"],
  tools: [],
};

input.on("line", (line) => {
  const command = JSON.parse(line);
  if (commandLog) fs.appendFileSync(commandLog, `${JSON.stringify({ method: command.method })}\n`);
  if (command.method === "initialize") {
    if (mode === "adversarial_error") {
      write({
        version: 1,
        type: "response",
        method: "initialize",
        request_id: command.request_id,
        ok: false,
        error_code: "internal_error",
        error: "synthetic credential marker <synthetic-path> private prompt",
      });
      return;
    }
    write({
      version: 1,
      type: "response",
      method: "initialize",
      request_id: command.request_id,
      ok: true,
      methods: mode === "incompatible"
        ? ["initialize", "catalog"]
        : ["initialize", "catalog", "start", "send", "cancel", "interaction"],
    });
    return;
  }
  if (command.method === "catalog") {
    write({
      version: 1,
      type: "response",
      method: "catalog",
      request_id: command.request_id,
      ok: true,
      catalog: {
        version: 1,
        cataloged_at: 1787875200,
        criteria: ["best-fit"],
        routes: [{
          id: "session-route",
          provider: "provider-a",
          connection: "local_session",
          label: "Local session",
          availability: "available",
          reason: "",
          charge_path: "subscription_quota",
          cost_certainty: "unknown",
          billing_note: "Account impact is unknown.",
          authenticated: true,
          capabilities: ["streaming", "resume", "cancel", "approvals", "collaboration"],
          models: [{ id: "example-model", provider: "provider-a", capabilities: ["llm"] }],
          default_model: "example-model",
          cataloged_at: 1787875200,
        }, ...(mode === "api_catalog" ? [{
          id: "synthetic-metered:api",
          provider: "synthetic-metered",
          connection: "api",
          label: "Synthetic metered API",
          availability: "available",
          reason: "Synthetic policy fixture; selection does not dispatch.",
          charge_path: "metered_api",
          cost_certainty: "known",
          billing_note: "Explicit API route; synthetic provider billing would apply.",
          authenticated: true,
          capabilities: ["resume", "cancel"],
          models: [{ id: "synthetic-metered/example-model", provider: "synthetic-metered", capabilities: ["llm"] }],
          default_model: "synthetic-metered/example-model",
          cataloged_at: 1787875200,
        }] : [])],
      },
    });
    if (mode === "controlled_post_catalog_exit") {
      if (!readyMarker || !releaseMarker) throw new Error("controlled exit markers are required");
      fs.writeFileSync(readyMarker, "ready");
      const release = setInterval(() => {
        if (!fs.existsSync(releaseMarker)) return;
        clearInterval(release);
        process.stderr.write("private bridge diagnostic".padEnd(140, "!"));
        process.exit(1);
      }, 5);
    }
    if (mode === "crash") process.nextTick(() => process.exit(17));
    if (mode === "malformed" || mode === "malformed_cleanup") {
      process.nextTick(() => process.stdout.write("not-json\n"));
    }
    return;
  }
  if (command.method === "start") {
    if (mode === "crash_on_start") {
      process.nextTick(() => process.exit(17));
      return;
    }
    const response = {
      version: 1, type: "response", method: "start", request_id: command.request_id, ok: true, run,
    };
    if (mode === "same_chunk_terminal") {
      const completed = { ...run, status: "done" };
      process.stdout.write(`${JSON.stringify(response)}\n${JSON.stringify({
        version: 1,
        type: "event",
        run: completed,
        event: {
          kind: "done",
          text: "",
          event_id: "terminal-1",
          sequence: 1,
          agent_run_id: "root-run",
          status: "completed",
        },
      })}\n`);
      return;
    }
    write(response);
    writeAcrossUtf8Boundary({
      version: 1,
      type: "event",
      run,
      event: {
        kind: "text",
        text: "streamed café answer",
        event_id: "event-1",
        sequence: 1,
        agent_run_id: "root-run",
      },
    }, "é");
    setTimeout(() => {
      write({
        version: 1,
        type: "event",
        run,
        event: {
          kind: "text",
          text: "duplicate must be ignored",
          event_id: "event-1",
          sequence: 1,
          agent_run_id: "root-run",
        },
      });
      const child = {
        ...run,
        run_id: "child-run",
        name: "child",
        kind: "provider_child",
        root_run_id: "root-run",
        parent_run_id: "root-run",
      };
      write({
        version: 1,
        type: "event",
        run: child,
        event: {
          kind: "text",
          text: "child event with the same provider-local id",
          event_id: "event-1",
          sequence: 1,
          agent_run_id: "child-run",
        },
      });
    }, 5);
    return;
  }
  if (command.method === "send") {
    write({ version: 1, type: "response", method: "send", request_id: command.request_id, ok: true, run });
    return;
  }
  if (command.method === "interaction") {
    write({ version: 1, type: "response", method: "interaction", request_id: command.request_id, ok: true, run });
    return;
  }
  if (command.method === "cancel") {
    write({
      version: 1,
      type: "response",
      method: "cancel",
      request_id: command.request_id,
      ok: true,
      run: { ...run, status: "stopped" },
    });
  }
});

input.on("close", () => {
  if (mode !== "cleanup" && mode !== "malformed_cleanup") return;
  setTimeout(() => {
    const marker = process.env.INTERACT_FAKE_CLEANUP_MARKER;
    if (marker) fs.writeFileSync(marker, "stdin closed");
    process.exit(0);
  }, 25);
});
