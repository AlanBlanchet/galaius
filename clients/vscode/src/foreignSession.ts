/** Your OWN sessions, readable — the provider-transcript half of the conversation view.
 *
 *  A foreign run's id IS its Claude session id, and Claude Code files every session under
 *  ~/.claude/projects/<cwd with "/" and "." as "-">/<session-id>.jsonl. Mapping that shape
 *  into ours is what turns a "your session" row from a grey dead thing into a place — the
 *  panel can SHOW the session, which is the half of "chat just like in claude code" a session
 *  interact did not start can honestly have (watching, not steering: it is already being
 *  steered, by you, in its window).
 *
 *  Runtime-import-free apart from node builtins, so node --test can load it directly — the
 *  same discipline as rail.ts (the loader demands ".ts" specifiers tsc refuses to emit).
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import type { AgentActivity, AgentRun } from "./agents";

/** Where the provider files one of your sessions. */
export function foreignTranscriptPath(
  run: Pick<AgentRun, "run_id" | "cwd">,
  home: string = os.homedir(),
): string | null {
  if (!run.cwd) return null;
  return path.join(home, ".claude", "projects", run.cwd.replace(/[/.]/g, "-"), `${run.run_id}.jsonl`);
}

/** The TAIL of a file. These transcripts grow to hundreds of MB, so reading the whole file per
 *  refresh would make the panel scale with the longest session on the machine; the last chunk
 *  is read and the first partial line dropped. */
function tailText(file: string, bytes: number): string | null {
  let fd: number;
  try {
    fd = fs.openSync(file, "r");
  } catch {
    return null;
  }
  try {
    const size = fs.fstatSync(fd).size;
    const start = Math.max(0, size - bytes);
    const buf = Buffer.alloc(size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    const text = buf.toString("utf8");
    return start === 0 ? text : text.slice(text.indexOf("\n") + 1);
  } catch {
    return null;
  } finally {
    fs.closeSync(fd);
  }
}

/** One of your own sessions' recent activity, mapped from the provider's transcript shape to
 *  ours — so the conversation view renders it with the exact grammar every other transcript
 *  gets. Only the shapes worth reading map; housekeeping lines are skipped. */
export function readForeignActivity(
  run: Pick<AgentRun, "run_id" | "cwd">,
  limit = 40,
  home?: string,
): AgentActivity[] {
  const file = foreignTranscriptPath(run, home);
  if (!file) return [];
  const text = tailText(file, limit > 100 ? 1 << 20 : 1 << 18);
  if (!text) return [];
  const out: AgentActivity[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    let raw: unknown;
    try {
      raw = JSON.parse(line);
    } catch {
      continue;
    }
    const t = raw as { type?: string; timestamp?: string;
      message?: { content?: unknown } };
    const at = t.timestamp ? Date.parse(t.timestamp) / 1000 : null;
    const stamp = Number.isFinite(at) ? at : null;
    if (t.type === "assistant" && Array.isArray(t.message?.content)) {
      for (const block of t.message.content as Array<Record<string, unknown>>) {
        if (block.type === "text" && String(block.text ?? "").trim()) {
          out.push({ kind: "text", text: String(block.text), at: stamp });
        } else if (block.type === "thinking" && String(block.thinking ?? "").trim()) {
          out.push({ kind: "thinking", text: String(block.thinking), at: stamp });
        } else if (block.type === "tool_use") {
          out.push({ kind: "tool", text: "", tool: String(block.name ?? ""),
                     tool_input: JSON.stringify(block.input ?? {}).slice(0, 400),
                     tool_id: String(block.id ?? ""), at: stamp });
        }
      }
    } else if (t.type === "user") {
      const content = t.message?.content;
      if (typeof content === "string") {
        if (content.trim()) out.push({ kind: "prompt", text: content, at: stamp });
      } else if (Array.isArray(content)) {
        for (const block of content as Array<Record<string, unknown>>) {
          if (block.type === "text" && String(block.text ?? "").trim()) {
            out.push({ kind: "prompt", text: String(block.text), at: stamp });
          } else if (block.type === "tool_result") {
            const inner = block.content;
            const said = typeof inner === "string"
              ? inner
              : Array.isArray(inner)
                ? (inner as Array<Record<string, unknown>>)
                    .filter((b) => b.type === "text").map((b) => String(b.text ?? "")).join("\n")
                : "";
            out.push({ kind: "tool_result", text: said.slice(0, 4000),
                       tool_id: String(block.tool_use_id ?? ""), at: stamp });
          }
        }
      }
    }
  }
  return out.slice(-Math.max(1, limit));
}

/** The FULL input or output of ONE tool call, out of a Claude-dialect JSONL — a supervised
 *  run's raw stream or a foreign session's transcript, which share the shape. The summarised
 *  events are clipped by design; the raw line holds everything, keyed by the vendor's
 *  tool_use id (the one stable pairing — a prefix match breaks on two identical commands).
 *
 *  The call is usually recent, so a large TAIL is scanned first and the whole file only when
 *  the id is not in it — these files reach hundreds of MB and this runs on a click.
 */
export function fullToolIO(file: string, toolId: string, side: "in" | "out"): string | null {
  if (!toolId) return null;
  const scan = (text: string): string | null => {
    for (const line of text.split("\n")) {
      if (!line.includes(toolId)) continue; // cheap gate before JSON.parse
      let raw: unknown;
      try {
        raw = JSON.parse(line);
      } catch {
        continue;
      }
      const t = raw as { type?: string; message?: { content?: unknown } };
      const blocks = Array.isArray(t.message?.content)
        ? (t.message.content as Array<Record<string, unknown>>) : [];
      if (side === "in" && t.type === "assistant") {
        for (const b of blocks) {
          if (b.type === "tool_use" && b.id === toolId) {
            return JSON.stringify(b.input ?? {}, null, 2);
          }
        }
      } else if (side === "out" && t.type === "user") {
        for (const b of blocks) {
          if (b.type === "tool_result" && b.tool_use_id === toolId) {
            const inner = b.content;
            if (typeof inner === "string") return inner;
            if (Array.isArray(inner)) {
              return (inner as Array<Record<string, unknown>>)
                .filter((x) => x.type === "text").map((x) => String(x.text ?? "")).join("\n");
            }
            return "";
          }
        }
      }
    }
    return null;
  };
  let size = 0;
  try {
    size = fs.statSync(file).size;
  } catch {
    return null;
  }
  const TAIL = 1 << 22;
  const fromTail = scan(tailText(file, TAIL) ?? "");
  if (fromTail !== null || size <= TAIL) return fromTail;
  try {
    return scan(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}
