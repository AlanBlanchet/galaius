/** One tool call's FULL input or output, as a READ-ONLY document.
 *
 *  The transcript shows a compact row per tool call — the summarised events are clipped by
 *  Python on purpose (300 chars of arguments, 2000 of result), because a 50k dump is scrolled
 *  past, not read. The WHOLE thing lives in the run's raw stream, keyed by the vendor's
 *  tool_use id, and opens here in its own tab — the same read-only scheme discipline as
 *  activityDocument.ts (an untitled document would nag about saving a log nobody wrote).
 */

export const IO_SCHEME = "interact-agent-io";

export type IoSide = "in" | "out";

/** The document path: the basename names the tab ("Bash.out.txt"), the ids make it fetchable. */
export function ioPath(runId: string, toolId: string, side: IoSide, tool: string): string {
  const safe = (tool || "tool").replace(/[^\w.-]+/g, "-");
  return `/${runId}/${toolId}/${safe}.${side}.txt`;
}

/** The ids back out of a document path — what the content provider is asked to render. */
export function ioFromPath(path: string): { runId: string; toolId: string; side: IoSide } | null {
  const parts = path.split("/").filter(Boolean);
  if (parts.length !== 3) return null;
  const side = /\.in\.txt$/.test(parts[2]) ? "in" : /\.out\.txt$/.test(parts[2]) ? "out" : null;
  if (!side) return null;
  return { runId: parts[0], toolId: parts[1], side };
}

/** Stored halves banked for calls the raw stream cannot look up — records that predate the
 *  vendor-id stamping. Keyed by a minted token that rides in the document path; the provider
 *  checks here before scanning any file. In-memory on purpose: the text came out of the
 *  webview one click ago, and a stale entry costs nothing but a few KB until reload. */
export const IO_INLINE = new Map<string, string>();

let minted = 0;

/** Route an open-in-tab request: a stamped call opens by id (the raw stream holds the whole
 *  thing); an unstamped one banks its STORED text under a minted key the provider serves
 *  back — clipped by the writer, but everything the record holds. Null when there is nothing
 *  to open at all. */
export function ioTarget(
  runId: string, toolId: string, side: IoSide, tool: string, text: string,
): string | null {
  if (toolId) return ioPath(runId, toolId, side, tool);
  if (!text) return null;
  const key = `inline${(++minted).toString(36)}`;
  IO_INLINE.set(key, text);
  return ioPath(runId, key, side, tool);
}
