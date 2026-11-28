/** Reading the agent-run registry that the Python side writes.
 *
 *  This process only ever READS: galaius agents (or an agent calling the MCP tools) owns
 *  spawning and stopping. The panel is a window onto that, which is why a run's status here is
 *  whatever Python derived from the live pid — the extension never guesses liveness itself.
 *
 *  Mirrors galaius.agents.registry (galaius/agents/registry.py);
 *  tests/config/test_paths.py binds the directory the two agree on.
 */
import * as fs from "fs";
import * as path from "path";

import { agentsDir } from "../paths";
import { readForeignActivity } from "./foreignSession";
import { livenessOf, runStatusOf, type RunStatus } from "./runStatus";
import type {
  AgentEvent as GeneratedAgentEvent,
  AgentRun as GeneratedAgentRun,
} from "../generated/types";
import { decodeAgentEvent, decodeAgentRun } from "../generated/types";
import { olderWritten } from "../billing/tokenCounts.ts";

/** Python owns every persisted field. The panel adds only the required display status, including
 * its synthetic declared roster row; no second wire contract lives here. */
export type AgentRun = Omit<GeneratedAgentRun, "status"> & { status: RunStatus };

const SAFE_RUN_ID = /^[A-Za-z0-9._:@+-]{1,160}$/;

/** Mirrors Python registry _safe_run_id; no persisted/provider id may become a path segment. */
function agentFile(runId: string, suffix: string): string | null {
  if (runId === "." || runId === ".." || !SAFE_RUN_ID.test(runId)) return null;
  return path.join(agentsDir(), `${runId}${suffix}`);
}

/** Did this run's own stream reach an end? Read from the normalised mirror, so it costs one small
 *  read and needs no knowledge of any vendor's dialect. */
function streamEnded(runId: string): boolean {
  if (!agentFile(runId, ".jsonl")) return false;
  return panelActivity.read(runId).events.slice(-4)
    .some((a) => a.kind === "done" || a.kind === "error");
}

/** Is that pid still there?
 *
 *  Node documents signal 0 as an existence probe that sends nothing — the check the Python side
 *  makes with the same intent. It only ever DOWNGRADES a claim: a record saying "running" whose
 *  process is gone is reported crashed. It never promotes anything, so a permission error (EPERM
 *  — the process exists but belongs to another user) correctly counts as alive.
 */
function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "EPERM";
  }
}

/** A registry file readers care about: never a hidden one (a mirror's own state files, tens of MB rewritten as it works;
 *  a file still being written). */
export const isRegistryFile = (name: string): boolean => !name.startsWith(".");

/** A reader of every registry file ending `suffix`, in name order, each parsed once per change of the file (inode, size,
 *  mtime) and held until it goes; one `parse` refuses is left out until it changes. The registry holds thousands of
 *  finished runs whose files never change again: parsing them all on each repaint took 1.4 to 1.6 s (5,616 records,
 *  64 MB, 2026-11-26). Only a listing and one `stat` per file are repeated. */
function changedOnly<T>(suffix: string, parse: (text: string) => T): (() => T[]) & { generation: () => number } {
  let held = new Map<string, { stamp: string; value?: T }>();
  /** Moves each time a file came, changed or went since the last read: what a caller's own derived result is kept against. */
  let generation = 0;
  const read = () => {
    const dir = agentsDir();
    let names: string[];
    try {
      names = fs.readdirSync(dir).filter((n) => isRegistryFile(n) && n.endsWith(suffix) && !n.endsWith(".queue.json"));
    } catch {
      if (held.size > 0) generation += 1;   // the folder went: what was kept of it goes too
      held = new Map();
      return []; // no registry yet — nothing has been spawned
    }
    const next: typeof held = new Map();
    const values: T[] = [];
    for (const name of names.sort()) {
      const file = path.join(dir, name);
      let entry: { stamp: string; value?: T };
      try {
        // Taken before the read: a write landing in between leaves an older stamp, so it is read again next time.
        const info = fs.statSync(file);
        const stamp = `${info.ino}:${info.size}:${info.mtimeMs}`;
        const known = held.get(file);
        if (known?.stamp === stamp) {
          entry = known;
        } else {
          const text = fs.readFileSync(file, "utf8");
          try {
            entry = { stamp, value: parse(text) };
          } catch {
            entry = { stamp };
          }
        }
      } catch {
        continue; // gone or unreadable now: looked at again next time
      }
      next.set(file, entry);
      if (entry.value !== undefined) values.push(entry.value);
    }
    if (next.size !== held.size || [...next].some(([file, entry]) => held.get(file) !== entry)) generation += 1;
    held = next;
    return values;
  };
  return Object.assign(read, { generation: () => generation });
}

const runRecords = changedOnly(".json", (text): AgentRun => {
  const raw = decodeAgentRun(olderWritten(JSON.parse(text)));
  if (!agentFile(raw.run_id, ".json")) throw new Error("unsafe run id");
  return { ...raw, run_id: raw.run_id, provider: raw.provider, name: raw.name, status: runStatusOf(raw.status) };
});

/** The status to show for a record. Liveness is the one field the file cannot vouch for, so a record saying it works is
 *  probed on every read. The agent's own stream is consulted too: a detached run's exit code is written by nobody, so the
 *  probe alone called every finished agent a crash at the moment it succeeded. */
function shownStatus(run: AgentRun): RunStatus {
  if (run.status !== "starting" && run.status !== "running") return run.status;
  return livenessOf(
    run.status,
    run.pid,
    typeof run.pid === "number" ? alive(run.pid) : false,
    run.status === "running" ? streamEnded(run.run_id) : false,
    // Did it ever say anything? A vanished run that wrote a transcript finished; one that
    // wrote nothing is what a crash looks like. Only consulted when the stream cannot tell.
    Boolean(run.last) || Boolean(run.usage?.output),
  );
}

/** Per timer `refreshLater` returned: when its burst began, or, once it fired, when that refresh started. */
const burst = new WeakMap<object, { first: number; fired: boolean }>();

/** A refresh asked `wait` ms after the latest of a burst of writes, and never later than `maxWait` after the start of the
 *  previous refresh (or after the burst's first write when none came lately): a run writing faster than `wait` kept a panel
 *  from refreshing until it stopped (8 s measured), then, counted from the first write, refreshed every 1 s plus its own
 *  render (0.69 a second; perf-critic 2026-11-28). `previous` is the timer this returned last; the new one replaces it. */
export function refreshLater(previous: ReturnType<typeof setTimeout> | undefined, run: () => void, wait: number, maxWait = 1000): ReturnType<typeof setTimeout> {
  const now = Date.now();
  let first = now;
  const before = previous ? burst.get(previous) : undefined;
  if (previous) clearTimeout(previous);
  if (before !== undefined && (!before.fired || now - before.first < maxWait)) first = before.first;
  const timer: ReturnType<typeof setTimeout> = setTimeout(() => {
    burst.set(timer, { first: Date.now(), fired: true });
    run();
  }, Math.max(0, Math.min(wait, first + maxWait - now)));
  burst.set(timer, { first, fired: false });
  return timer;
}

let readThisTurn: AgentRun[] | undefined;

/** Every run record on disk, oldest first. A corrupt record is skipped rather than hiding the
 *  rest — a half-written file during a crash must not blank the whole panel. One read serves every caller of the same
 *  synchronous turn (a chat refresh asked for it 4 to 8 times, 0.5 s of CPU each, perf-critic 2026-11-28); the next turn reads again. */
export function readAgentRuns(): AgentRun[] {
  if (readThisTurn === undefined) {
    readThisTurn = readAllRuns();
    setImmediate(() => { readThisTurn = undefined; });
  }
  return [...readThisTurn];
}

function readAllRuns(): AgentRun[] {
  const runs: AgentRun[] = [];
  for (const record of runRecords()) {
    try {
      runs.push({ ...record, status: shownStatus(record) });
    } catch {
      continue;
    }
  }
  return runs.sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0));
}

/** Totals for the status line: how many are live, and the API-equivalent value consumed. */
export function summarise(runs: AgentRun[]): { live: number; cost: number } {
  return {
    live: runs.filter((r) => r.status === "running").length,
    cost: runs.reduce((sum, r) => sum + (r.cost_usd ?? 0), 0),
  };
}

/** Indent each run under the one that spawned it, so a cross-provider team reads as a tree.
 *  Depth is capped so a cycle in malformed data can't loop forever. */
export function withDepth(runs: AgentRun[]): { run: AgentRun; depth: number }[] {
  const byId = new Map(runs.map((r) => [r.run_id, r]));
  return runs.map((run) => {
    let depth = 0;
    let cursor = run.parent_run_id;
    const seen = new Set<string>([run.run_id]);
    while (cursor && byId.has(cursor) && !seen.has(cursor) && depth < 8) {
      seen.add(cursor);
      depth += 1;
      cursor = byId.get(cursor)!.parent_run_id;
    }
    return { run, depth };
  });
}

/** The generated normalized event, with the display body default Python has always written. */
export type AgentActivity = GeneratedAgentEvent & Required<Pick<GeneratedAgentEvent, "text">>;

function decodeActivity(line: string): AgentActivity | null {
  try {
    const event = decodeAgentEvent(olderWritten(JSON.parse(line)));
    return { ...event, text: event.text ?? "" };
  } catch {
    return null;
  }
}

/** Whether a mirror line holds the words so far of a text block still being written (galaius `partial`: several per answer, the
 *  closing `text` line holds the whole). Dropped before any window of lines is cut: they would fill it, and the panel reads the answer once. */
const PARTIAL_LINE = '"kind":"partial"';
const isPartialLine = (line: string): boolean => line.includes(PARTIAL_LINE);

/** The bytes the first read of a run's mirror takes from its end, then the wider one when partial lines in those may be hiding
 *  lines the panel reads (an answer being written, or a long one just closed, fills the first). */
const COLD_TAIL_BYTES = [127_000, 4_000_000];

/** A run's recent activity, oldest last. Bounded by limit because a long run's transcript is
 *  unbounded and the panel only ever shows a tail — reading it all to display ten lines would
 *  make every refresh scale with the longest-running agent. */
export function readAgentActivity(runId: string, limit = 40): AgentActivity[] {
  const file = agentFile(runId, ".jsonl");
  if (!file) return [];
  let text: string;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return []; // no normalised stream for this run (yet)
  }
  const out: AgentActivity[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim() || isPartialLine(line)) continue;
    try {
      const event = decodeActivity(line);
      if (event) out.push(event);
    } catch {
      continue;
    }
  }
  return out.slice(-Math.max(1, limit));
}

interface ActivityRead {
  events: AgentActivity[];
  bytesRead: number;
  parseInvocations: number;
}

class AgentActivityReader {
  private readonly states = new Map<string, {
    offset: number;
    device: bigint;
    inode: bigint;
    anchor: Buffer;
    events: AgentActivity[];
  }>();

  constructor(private readonly eventLimit: number, private readonly transcriptLimit: number) {}

  public read(runId: string): ActivityRead {
    const file = agentFile(runId, ".jsonl");
    if (!file) return { events: [], bytesRead: 0, parseInvocations: 0 };
    let descriptor: number;
    try { descriptor = fs.openSync(file, "r"); } catch { return { events: [], bytesRead: 0, parseInvocations: 0 }; }
    try {
      const stat = fs.fstatSync(descriptor, { bigint: true });
      const size = Number(stat.size);
      const prior = this.states.get(runId);
      let continued = false;
      let anchorBytes = 0;
      if (prior !== undefined && prior.device === stat.dev && prior.inode === stat.ino
          && size >= prior.offset) {
        continued = true;
        if (prior.anchor.length) {
          const anchor = Buffer.alloc(prior.anchor.length);
          anchorBytes = fs.readSync(
            descriptor, anchor, 0, anchor.length, prior.offset - prior.anchor.length,
          );
          continued = anchorBytes === prior.anchor.length && anchor.equals(prior.anchor);
        }
      }
      let start = 0;
      let bytesRead = 0;
      let text = "";
      let lastNewline = -1;
      let lines: string[] = [];
      for (const tail of continued ? [0] : COLD_TAIL_BYTES) {
        start = continued ? prior!.offset : Math.max(0, size - tail);
        const buffer = Buffer.alloc(size - start);
        bytesRead = fs.readSync(descriptor, buffer, 0, buffer.length, start);
        text = buffer.subarray(0, bytesRead).toString("utf8");
        lastNewline = text.lastIndexOf("\n");
        const complete = lastNewline < 0 ? "" : text.slice(0, lastNewline);
        let rawLines = complete.split("\n");
        if (start > 0 && !continued) rawLines = rawLines.slice(1);
        lines = rawLines.filter((line) => line.trim() && !isPartialLine(line)).slice(-this.eventLimit);
        // Wider only while partial lines may be hiding lines the window would hold (a tail with none is read once).
        if (lines.length >= this.eventLimit || start === 0 || !rawLines.some(isPartialLine)) break;
      }
      const parsed: AgentActivity[] = [];
      for (const line of lines) {
        const event = decodeActivity(line);
        if (event) parsed.push(event);
      }
      const base = continued ? prior!.events : [];
      const seen = new Set(base.map((event) => event.event_id).filter(Boolean));
      const fresh = parsed.filter((event) => !event.event_id || !seen.has(event.event_id));
      const retained = [...base, ...fresh].slice(-this.transcriptLimit);
      const offset = start + (lastNewline < 0 ? 0 : Buffer.byteLength(text.slice(0, lastNewline + 1)));
      const anchorLength = Math.min(64, offset);
      const anchor = Buffer.alloc(anchorLength);
      const anchorRead = anchorLength
        ? fs.readSync(descriptor, anchor, 0, anchorLength, offset - anchorLength)
        : 0;
      this.states.set(runId, {
        offset,
        device: stat.dev,
        inode: stat.ino,
        anchor: anchor.subarray(0, anchorRead),
        events: retained,
      });
      return {
        events: retained.slice(-this.eventLimit),
        bytesRead: bytesRead + anchorBytes + anchorRead,
        parseInvocations: lines.length,
      };
    } finally {
      fs.closeSync(descriptor);
    }
  }
}

export function createAgentActivityReader(
  limits: { eventLimit: number; transcriptLimit: number },
): AgentActivityReader {
  return new AgentActivityReader(limits.eventLimit, limits.transcriptLimit);
}

const panelActivity = createAgentActivityReader({ eventLimit: 40, transcriptLimit: 300 });

/** A run's activity, whichever kind of run it is: galaius's own normalised stream, or — for a
 *  session galaius did not start — the provider's transcript, mapped. One call site, one rule. */
export function activityOf(run: AgentRun, limit = 40): AgentActivity[] {
  return run.status === "foreign"
    ? readForeignActivity(run, limit)
    : panelActivity.read(run.run_id).events.slice(-Math.max(1, limit));
}

/** Where the child wrote its OWN stream, verbatim — full tool inputs and outputs live here.
 *  Mirrors Python's raw_events_path. */
export function rawEventsPath(runId: string): string | null {
  return agentFile(runId, ".raw.jsonl");
}

/** One agent addressing another. Written by Python to <run_id>.messages.jsonl on BOTH sides,
 *  so this dedupes — the same exchange appears in the sender's file and the recipient's. */
export interface AgentMessage {
  from_run: string;
  to_run: string;
  text: string;
  /** When the exchange was recorded. Absent on messages written before it was stamped. */
  at?: number | null;
}

const messageFiles = changedOnly(".messages.jsonl", (text): AgentMessage[] => {
  const messages: AgentMessage[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const raw = JSON.parse(line);
      if (!raw?.from_run || !raw?.to_run) continue;
      messages.push({ from_run: raw.from_run, to_run: raw.to_run, text: String(raw.text ?? "") });
    } catch {
      continue;
    }
  }
  return messages;
});

/** Every recorded exchange, in file order. These are the edges a sequence view draws; without
 *  them the panel can only show monologues side by side. */
let messages: { generation: number; value: AgentMessage[] } | undefined;

export function readAgentMessages(): AgentMessage[] {
  const files = messageFiles();
  const generation = messageFiles.generation();
  // 50 MB of messages (9,222 of them) were deduplicated again on every read: 180 ms of the 267 (perf-critic 2026-11-28).
  if (messages !== undefined && messages.generation === generation) return [...messages.value];
  const seen = new Set<string>();
  const out: AgentMessage[] = [];
  for (const message of files.flat()) {
    const key = `${message.from_run} ${message.to_run} ${message.text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ...message });
  }
  messages = { generation, value: out };
  return [...out];
}
