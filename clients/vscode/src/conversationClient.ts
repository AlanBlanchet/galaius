/** One extension-window-owned newline-JSON connection to `interact agents console`.
 *
 * Prompts and provider payloads travel over stdin/stdout, never argv or logs.  The process is
 * deliberately one-shot: after EOF, malformed output, protocol mismatch or timeout the client is
 * terminal.  An ambiguous turn must require a new user action rather than an automatic retry.
 */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { StringDecoder } from "node:string_decoder";

import type {
  AgentEvent,
  AgentRun,
  ConversationCatalog,
  ConversationCommand,
  ConversationRequest,
  ConversationResponse,
  InteractionSubmission,
} from "./generated/types";
import {
  decodeConversationResponse,
  decodeConversationStreamEvent,
} from "./generated/types";

const PROTOCOL_VERSION = 1 as const;
const REQUIRED_METHODS = ["catalog", "start", "send", "cancel", "interaction"] as const;
const MAX_LINE_BYTES = 8 << 20;
const MAX_OBSERVED_EVENTS = 4096;
const DEFAULT_TIMEOUT_MS = 15_000;
/** Longer than the Python owner's 2s graceful + 1s terminate subprocess shutdown budget. */
export const CONVERSATION_SHUTDOWN_GRACE_MS = 4_000;

export type ConversationClientState = "idle" | "connecting" | "ready" | "crashed" | "closed";

export interface ConversationProcessSpec {
  command: string;
  args: readonly string[];
  cwd: string;
  env?: NodeJS.ProcessEnv;
  timeoutMs?: number;
  origin?: "project_checkout" | "local_path";
}

export interface ConversationClientHandlers {
  onEvent?: (run: AgentRun, event: AgentEvent) => void;
  onError?: (message: string) => void;
  /** Untrusted child stderr, for the extension's LOG — never the UI. See `reportDiagnostic`. */
  onDiagnostic?: (line: string) => void;
  onState?: (state: ConversationClientState) => void;
}

export interface ConversationClient {
  catalog(): Promise<ConversationCatalog>;
  start(request: ConversationRequest): Promise<AgentRun>;
  send(runId: string, prompt: string): Promise<AgentRun>;
  cancel(runId: string): Promise<AgentRun>;
  interact(runId: string, submission: InteractionSubmission): Promise<AgentRun>;
  state(): ConversationClientState;
  dispose(): void;
}

/** Existing supervised processes keep the established one-shot CLI send contract. */
export function usesConversationTransport(run: Pick<AgentRun, "kind">): boolean {
  return run.kind === "conversation";
}

type SuccessfulConversationResponse = Extract<ConversationResponse, { ok: true }>;

type Pending = {
  method: ConversationCommand["method"];
  resolve: (response: SuccessfulConversationResponse) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
};

/** Build the stateful transport as one closure instead of introducing another public hierarchy. */
export function createConversationClient(
  spec: ConversationProcessSpec,
  handlers: ConversationClientHandlers = {},
): ConversationClient {
  let child: ChildProcessWithoutNullStreams | undefined;
  let currentState: ConversationClientState = "idle";
  let terminalMessage = "The conversation bridge stopped.";
  let buffer = "";
  let nextRequest = 1;
  let stderrBytes = 0;
  let shutdownStarted = false;
  let connecting: Promise<void> | undefined;
  const pending = new Map<string, Pending>();
  const observedEvents = new Set<string>();
  const decoder = new StringDecoder("utf8");

  const stopProcess = (process: ChildProcessWithoutNullStreams | undefined): void => {
    if (!process || shutdownStarted) return;
    shutdownStarted = true;
    try {
      process.stdin.end();
    } catch {
      process.kill();
      return;
    }
    const escalation = setTimeout(() => {
      if (process.exitCode === null && process.signalCode === null) process.kill();
    }, CONVERSATION_SHUTDOWN_GRACE_MS);
    escalation.unref();
    process.once("exit", () => clearTimeout(escalation));
  };

  const setState = (state: ConversationClientState): void => {
    currentState = state;
    handlers.onState?.(state);
  };

  const boundedProtocolDetail = (value: unknown): string => String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 500);

  const fail = (message: string): void => {
    if (currentState === "closed" || currentState === "crashed") return;
    terminalMessage = boundedProtocolDetail(message) || "The conversation bridge stopped.";
    setState("crashed");
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(new Error(terminalMessage));
    }
    pending.clear();
    handlers.onError?.(terminalMessage);
    stopProcess(child);
  };

  const responseError = (errorCode: unknown): Error => {
    const details: Record<string, string> = {
      invalid_request: "The conversation request was rejected.",
      unsupported_version: "The conversation protocol is incompatible.",
      unavailable: "The selected route is unavailable.",
      unauthenticated: "The selected route is not authenticated.",
      incompatible: "The selected route is incompatible.",
      not_found: "The conversation or model was not found.",
      conflict: "Another turn is already active.",
      provider_failed: "The provider request failed.",
      cancelled: "The conversation request was cancelled.",
      internal_error: "The local conversation host failed.",
    };
    const offered = boundedProtocolDetail(errorCode);
    const code = offered in details ? offered : "conversation_error";
    return new Error(`${code}: ${details[code] ?? "The conversation request failed."}`);
  };

  const receive = (line: string): void => {
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      fail("The conversation bridge emitted malformed JSON.");
      return;
    }
    if (!value || typeof value !== "object") {
      fail("The conversation bridge emitted a non-object message.");
      return;
    }
    const message = value as Record<string, unknown>;
    if (message.version !== PROTOCOL_VERSION) {
      fail("The conversation bridge protocol version is incompatible.");
      return;
    }
    if (message.type === "event") {
      let eventMessage;
      try {
        eventMessage = decodeConversationStreamEvent(value);
      } catch {
        fail("The conversation bridge emitted an invalid event.");
        return;
      }
      const eventId = eventMessage.event.event_id;
      const eventKey = eventId ? `${eventMessage.run.run_id}\0${eventId}` : "";
      if (eventKey && observedEvents.has(eventKey)) return;
      if (eventKey) {
        if (observedEvents.size >= MAX_OBSERVED_EVENTS) {
          const oldest = observedEvents.values().next().value;
          if (oldest !== undefined) observedEvents.delete(oldest);
        }
        observedEvents.add(eventKey);
      }
      // A start response and its first provider event can share one stdout chunk. The public
      // `start()` path has more than one Promise continuation (exchange → request → caller), so a
      // single microtask can still overtake the caller. The check-phase callback runs after that
      // microtask chain and keeps event callbacks FIFO without delaying the subprocess parser.
      setImmediate(() => handlers.onEvent?.(eventMessage.run, eventMessage.event));
      return;
    }
    let response;
    try {
      response = decodeConversationResponse(value);
    } catch {
      fail("The conversation bridge emitted an invalid response.");
      return;
    }
    const request = pending.get(response.request_id);
    if (!request) {
      fail("The conversation bridge replied to an unknown or completed request.");
      return;
    }
    if (response.ok === false) {
      pending.delete(response.request_id);
      clearTimeout(request.timer);
      request.reject(responseError(response.error_code));
      return;
    }
    if (response.method !== request.method) {
      fail("The conversation bridge emitted a mismatched response.");
      return;
    }
    pending.delete(response.request_id);
    clearTimeout(request.timer);
    request.resolve(response);
  };

  const accept = (chunk: Buffer | string): void => {
    buffer += typeof chunk === "string" ? chunk : decoder.write(chunk);
    if (Buffer.byteLength(buffer) > MAX_LINE_BYTES && !buffer.includes("\n")) {
      fail("The conversation bridge emitted an oversized message.");
      return;
    }
    let newline = buffer.indexOf("\n");
    while (newline >= 0 && currentState !== "crashed") {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      if (Buffer.byteLength(line) > MAX_LINE_BYTES) {
        fail("The conversation bridge emitted an oversized message.");
        return;
      }
      if (line.trim()) receive(line);
      newline = buffer.indexOf("\n");
    }
  };

  /** A bounded quote of the child's stderr — enough to name a cause, never a log dump. */
  const STDERR_TAIL_MAX = 600;
  let stderrTail = "";
  /** The tail, for the LOG only. A count alone is useless — "exit 1; 638 stderr bytes" proves an
   *  explanation existed and withholds it, and that text is what identified a real version skew.
   *  But a child's stderr is untrusted and may carry paths or secrets, so it never reaches the UI:
   *  `onDiagnostic` goes to the extension's output channel, `onError` to the user. */
  const reportDiagnostic = (ending: string): void => {
    const text = stderrTail.replace(/\s+/g, " ").trim();
    handlers.onDiagnostic?.(text
      ? `conversation bridge ${ending}; stderr tail: ${text}`
      : `conversation bridge ${ending}; nothing on stderr`);
  };

  const launch = (): ChildProcessWithoutNullStreams => {
    shutdownStarted = false;
    const process = spawn(spec.command, [...spec.args], {
      cwd: spec.cwd,
      env: spec.env,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    process.stdout.on("data", accept);
    // Bounded: stderr is untrusted text from a child process, and a crash loop can run long. See
    // reportDiagnostic above for why the tail is kept at all.
    process.stderr.on("data", (chunk: Buffer | string) => {
      stderrBytes += Buffer.byteLength(chunk);
      if (stderrTail.length < STDERR_TAIL_MAX) {
        stderrTail = (stderrTail + String(chunk)).slice(-STDERR_TAIL_MAX);
      }
    });
    process.on("error", (error) => fail((error as NodeJS.ErrnoException).code === "ENOENT"
      // NAME the program that is missing. A project-checkout spawn runs `uv run --directory …`,
      // so a missing `uv` reported "Interact is not installed" — confident, wrong advice beside a
      // RELOAD BRIDGE button, sending the reader to reinstall something that was already there.
      ? `${spec.command} was not found. Install it, then reload the window.`
      : "The conversation bridge could not start. Reload the window to try again."));
    process.on("exit", (code, signal) => {
      if (currentState === "closed") return;
      const ending = code === null ? `signal ${boundedProtocolDetail(signal) || "unknown"}` : `exit ${code}`;
      const beforeInitialization = currentState === "connecting";
      reportDiagnostic(ending);
      const origin = spec.origin === "project_checkout" ? "project checkout" : "verified local executable";
      fail(beforeInitialization
        ? `The conversation bridge exited before initialization (${ending}; ${stderrBytes} stderr bytes) via the ${origin}. Reload the window to try again.`
        : `The conversation bridge stopped unexpectedly (${ending}; ${stderrBytes} stderr bytes) via the ${origin}. Reload the window to try again.`);
    });
    return process;
  };

  const exchange = (command: ConversationCommand): Promise<SuccessfulConversationResponse> => {
    if (!child || currentState === "crashed" || currentState === "closed") {
      return Promise.reject(new Error(terminalMessage));
    }
    const requestId = command.request_id;
    return new Promise<SuccessfulConversationResponse>((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        const message = `The conversation bridge timed out during ${command.method}; it was not retried.`;
        reject(new Error(message));
        fail(message);
      }, spec.timeoutMs ?? DEFAULT_TIMEOUT_MS);
      pending.set(requestId, { method: command.method, resolve, reject, timer });
      try {
        child!.stdin.write(`${JSON.stringify(command)}\n`);
      } catch {
        pending.delete(requestId);
        clearTimeout(timer);
        const message = "The conversation bridge write failed.";
        reject(new Error(message));
        fail(message);
      }
    });
  };

  const connect = (): Promise<void> => {
    if (currentState === "ready") return Promise.resolve();
    if (currentState === "crashed" || currentState === "closed") {
      return Promise.reject(new Error(terminalMessage));
    }
    if (connecting) return connecting;
    setState("connecting");
    child = launch();
    connecting = exchange({
      version: PROTOCOL_VERSION,
      request_id: `extension-${nextRequest++}`,
      method: "initialize",
    })
      .then((response) => {
        if (response.method !== "initialize") {
          throw new Error("The conversation bridge returned the wrong initialization response.");
        }
        const methods = new Set(response.methods);
        const missing = REQUIRED_METHODS.filter((method) => !methods.has(method));
        if (missing.length) {
          throw new Error("The conversation bridge is incompatible with this extension. " +
            `Missing methods: ${missing.join(", ")}. Update interact, then reload the window.`);
        }
        setState("ready");
      })
      .catch((error: unknown) => {
        const message = boundedProtocolDetail(error instanceof Error ? error.message : error);
        fail(message);
        throw error;
      });
    return connecting;
  };

  const request = async (
    command: (requestId: string) => ConversationCommand,
  ): Promise<SuccessfulConversationResponse> => {
    await connect();
    return exchange(command(`extension-${nextRequest++}`));
  };

  const requireRun = (response: SuccessfulConversationResponse): AgentRun => {
    if (response.method === "initialize" || response.method === "catalog") {
      throw new Error("The conversation bridge returned the wrong run response.");
    }
    return response.run;
  };

  return {
    async catalog(): Promise<ConversationCatalog> {
      const response = await request((requestId) => ({
        version: PROTOCOL_VERSION,
        request_id: requestId,
        method: "catalog",
      }));
      if (response.method !== "catalog") {
        throw new Error("The conversation bridge returned the wrong catalog response.");
      }
      return response.catalog;
    },
    async start(conversation: ConversationRequest): Promise<AgentRun> {
      return requireRun(await request((requestId) => ({
        version: PROTOCOL_VERSION,
        request_id: requestId,
        method: "start",
        request: conversation,
      })));
    },
    async send(runId: string, prompt: string): Promise<AgentRun> {
      return requireRun(await request((requestId) => ({
        version: PROTOCOL_VERSION,
        request_id: requestId,
        method: "send",
        run_id: runId,
        prompt,
      })));
    },
    async cancel(runId: string): Promise<AgentRun> {
      return requireRun(await request((requestId) => ({
        version: PROTOCOL_VERSION,
        request_id: requestId,
        method: "cancel",
        run_id: runId,
      })));
    },
    async interact(runId: string, submission: InteractionSubmission): Promise<AgentRun> {
      return requireRun(await request((requestId) => ({
        version: PROTOCOL_VERSION,
        request_id: requestId,
        method: "interaction",
        run_id: runId,
        submission,
      })));
    },
    state: () => currentState,
    dispose(): void {
      if (currentState === "closed") return;
      setState("closed");
      terminalMessage = "The conversation bridge was closed.";
      for (const active of pending.values()) {
        clearTimeout(active.timer);
        active.reject(new Error(terminalMessage));
      }
      pending.clear();
      const closing = child;
      child = undefined;
      stopProcess(closing);
    },
  };
}
