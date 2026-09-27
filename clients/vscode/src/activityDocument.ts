/** The agent-activity log as a READ-ONLY document.
 *
 *  This used to open through workspace.openTextDocument({content}), which produces an UNTITLED
 *  document — and VS Code treats those as dirty. So glancing at what an agent did left an unsaved
 *  file behind, and closing it asked the user to save a log they never wrote.
 *
 *  A document served by a TextDocumentContentProvider under our own scheme is read-only by
 *  construction: there is nothing to save, and nothing to prompt about.
 */

export const ACTIVITY_SCHEME = "interact-agent-activity";

/** One recorded step of an agent run, as the panel reads it. */
export interface Activity {
  kind: string;
  /** Python leaves this null (not absent) when a step is not a tool call. */
  tool?: string | null;
  text?: string;
}

/** The document path for a run: the name makes the tab identifiable, the id makes it fetchable. */
export function activityPath(runId: string, name: string): string {
  const safe = name.replace(/[^\w.-]+/g, "-");
  return `/${safe}/${runId}`;
}

/** The run id back out of a document path — what the content provider is asked to render. */
export function runIdFromPath(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? "";
}

/** The log body: one aligned line per step, or a plain sentence when there is nothing yet. */
export function formatActivity(activity: Activity[]): string {
  if (!activity.length) return "(no activity recorded yet)";
  return activity.map((a) => `${a.kind.padEnd(12)}${a.tool ?? a.text ?? ""}`).join("\n");
}
