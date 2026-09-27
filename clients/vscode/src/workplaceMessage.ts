/** Which worker a message from the workplace names, if any.
 *
 *  Two shapes are accepted because two renderers post them: the pixel-art scene sends
 *  select/run_id, the plain fallback focus/runId. Handling only one was why clicking a
 *  worker did nothing — both halves had the hook, they just never agreed on the word for it.
 */
export function selectedRunId(msg: unknown): string | null {
  if (!msg || typeof msg !== "object") return null;
  const m = msg as { type?: unknown; run_id?: unknown; runId?: unknown };
  const id = m.type === "select" ? m.run_id : m.type === "focus" ? m.runId : null;
  return typeof id === "string" && id ? id : null;
}
