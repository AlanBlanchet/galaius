/** Whether a run is really still going.
 *
 *  Liveness is the one field a record cannot vouch for: a crash leaves "running" behind with
 *  nothing to correct it, and a row that spins forever is worse than no row.
 *
 *  But the probe alone is not enough. interact agents spawn returns as soon as the agent is
 *  alive, so nobody is left waiting to write its exit code — the record still says "running" long
 *  after the process is gone. Reading that as a crash made every detached agent flash a warning
 *  icon at the very moment it succeeded. The agent's OWN stream settles it: a stream that reached
 *  its end is a run that finished, whoever failed to record the fact.
 *
 *  When the stream cannot settle it either, the answer is genuinely UNKNOWN, and the two ways of
 *  being wrong are not equal. Python heals these records from the raw stream and is the authority,
 *  but it only does so while a current interact server is running — so this fallback is what the
 *  panel shows in the gap, and it was showing a red "crashed" warning over an agent that had
 *  returned its answer and exited cleanly. Claiming a failure that did not happen is the worse
 *  error: it sends someone to read a transcript for a problem that is not there.
 *
 *  So a crash now has to be EVIDENCED by the run having produced nothing at all. A run that wrote
 *  a transcript and then vanished is reported finished; one that vanished having said nothing is
 *  the shape a real crash leaves.
 */
import type { AgentRun as GeneratedAgentRun } from "./generated/types";

/** Python owns persisted status vocabulary; the panel adds only its roster-only synthetic row. */
export type RunStatus = NonNullable<GeneratedAgentRun["status"]> | "declared";

const RUN_STATUSES = [
  "starting", "running", "waiting", "done", "failed", "cancelled", "crashed", "stopped",
  "foreign", "declared",
] as const satisfies readonly RunStatus[];

/** Old/corrupt records remain readable without blessing arbitrary strings as a finite status. */
function isRunStatus(value: unknown): value is RunStatus {
  return typeof value === "string" && RUN_STATUSES.some((status) => status === value);
}

export function runStatusOf(value: unknown): RunStatus {
  return isRunStatus(value) ? value : "running";
}

export function livenessOf(
  recorded: RunStatus,
  pid: number | null | undefined,
  processAlive: boolean,
  streamEnded: boolean,
  producedOutput: boolean = false,
): RunStatus {
  if (recorded !== "starting" && recorded !== "running") return recorded;
  if (typeof pid !== "number") return recorded; // nothing to probe — a crash would be invented
  if (processAlive) return recorded;
  if (streamEnded) return "done";
  return producedOutput ? "done" : "crashed";
}
