/** Reading the agent-run registry that the Python side writes.
 *
 *  This process only ever READS: interact agents (or an agent calling the MCP tools) owns
 *  spawning and stopping. The panel is a window onto that, which is why a run's status here is
 *  whatever Python derived from the live pid — the extension never guesses liveness itself.
 *
 *  Mirrors interact.agents.registry (src/interact/agents/registry.py);
 *  tests/test_paths.py binds the directory the two agree on.
 */
import * as fs from "fs";
import * as path from "path";

import { agentsDir } from "./paths";
import { readForeignActivity } from "./foreignSession";
import { livenessOf, runStatusOf, type RunStatus } from "./runStatus";
import type {
  AgentEvent as GeneratedAgentEvent,
  AgentRun as GeneratedAgentRun,
} from "./generated/types";
import { decodeAgentEvent, decodeAgentRun } from "./generated/types";

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

/** Every run record on disk, oldest first. A corrupt record is skipped rather than hiding the
 *  rest — a half-written file during a crash must not blank the whole panel. */
export function readAgentRuns(): AgentRun[] {
  const dir = agentsDir();
  let names: string[];
  try {
    names = fs.readdirSync(dir).filter((n) => n.endsWith(".json"));
  } catch {
    return []; // no registry yet — nothing has been spawned
  }
  const runs: AgentRun[] = [];
  for (const name of names) {
    try {
      const raw = decodeAgentRun(JSON.parse(fs.readFileSync(path.join(dir, name), "utf8")));
      if (!agentFile(raw.run_id, ".json")) continue;
      const run: AgentRun = {
        ...raw,
        run_id: raw.run_id,
        provider: raw.provider,
        name: raw.name,
        status: runStatusOf(raw.status),
      };
      // Liveness is the one field the file cannot vouch for. The agent's own stream is consulted
      // too: a detached run's exit code is written by nobody, so the probe alone called every
      // finished agent a crash at the moment it succeeded.
      run.status = livenessOf(
        run.status,
        run.pid,
        typeof run.pid === "number" ? alive(run.pid) : false,
        run.status === "running" ? streamEnded(run.run_id) : false,
        // Did it ever say anything? A vanished run that wrote a transcript finished; one that
        // wrote nothing is what a crash looks like. Only consulted when the stream cannot tell.
        Boolean(run.last) || Boolean(run.output_tokens),
      );
      runs.push(run);
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
    const event = decodeAgentEvent(JSON.parse(line));
    return { ...event, text: event.text ?? "" };
  } catch {
    return null;
  }
}

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
    if (!line.trim()) continue;
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
      const start = continued ? prior!.offset : Math.max(0, size - 127_000);
      const buffer = Buffer.alloc(size - start);
      const bytesRead = fs.readSync(descriptor, buffer, 0, buffer.length, start);
      const text = buffer.subarray(0, bytesRead).toString("utf8");
      const lastNewline = text.lastIndexOf("\n");
      const complete = lastNewline < 0 ? "" : text.slice(0, lastNewline);
      let rawLines = complete.split("\n");
      if (start > 0 && !continued) rawLines = rawLines.slice(1);
      const lines = rawLines.filter((line) => line.trim()).slice(-this.eventLimit);
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

/** A run's activity, whichever kind of run it is: interact's own normalised stream, or — for a
 *  session interact did not start — the provider's transcript, mapped. One call site, one rule. */
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

/** Every recorded exchange, in file order. These are the edges a sequence view draws; without
 *  them the panel can only show monologues side by side. */
export function readAgentMessages(): AgentMessage[] {
  const dir = agentsDir();
  let names: string[];
  try {
    names = fs.readdirSync(dir).filter((n) => n.endsWith(".messages.jsonl"));
  } catch {
    return [];
  }
  const seen = new Set<string>();
  const out: AgentMessage[] = [];
  for (const name of names.sort()) {
    let text: string;
    try {
      text = fs.readFileSync(path.join(dir, name), "utf8");
    } catch {
      continue;
    }
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      try {
        const raw = JSON.parse(line);
        if (!raw?.from_run || !raw?.to_run) continue;
        const key = `${raw.from_run} ${raw.to_run} ${raw.text ?? ""}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ from_run: raw.from_run, to_run: raw.to_run, text: String(raw.text ?? "") });
      } catch {
        continue;
      }
    }
  }
  return out;
}
