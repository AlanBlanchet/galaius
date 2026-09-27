/** One way to ask the interact CLI a question.
 *
 *  This call was written ten times across the extension with ten different levels of care — one
 *  set a timeout, none set maxBuffer, several dropped the error entirely. That divergence isn't
 *  cosmetic: a hung binary with no timeout leaves whatever guard the caller set stuck forever, and
 *  a swallowed error is indistinguishable from a legitimate empty answer, so a panel caches
 *  "nothing" and re-caches "nothing" every refresh with no line anywhere saying why.
 *
 *  So: always a timeout, always a buffer ceiling, always a single place that can log. Never
 *  rejects — every caller here wants "what did it say, and did it work", not an exception in a
 *  repaint path.
 */
import { execFile } from "child_process";

export interface CliResult {
  stdout: string;
  /** What went wrong, in one line, or null. Distinguishes "it said nothing" from "it failed". */
  error: string | null;
}

/** Ten seconds. Long enough for a cold start on a loaded machine, short enough that a wedged
 *  binary doesn't silently disable a feature until the window is closed. */
const TIMEOUT_MS = 10_000;
/** 8 MB. A registry answer is kilobytes; anything approaching this is a bug, and the default 1 MB
 *  truncates silently, which reads as corrupt output rather than as too much of it. */
const MAX_BUFFER = 8 << 20;

export function interactCli(args: readonly string[]): Promise<CliResult> {
  return new Promise<CliResult>((resolve) => {
    try {
      execFile(
        "interact", [...args],
        { timeout: TIMEOUT_MS, maxBuffer: MAX_BUFFER, windowsHide: true },
        (err, stdout, stderr) => {
          resolve({
            stdout: stdout ?? "",
            error: err
              ? ((stderr || "").trim() || err.message || `interact ${args[0]} failed`)
              : null,
          });
        },
      );
    } catch (err) {
      // execFile itself can throw synchronously (a bad argument shape). Resolving rather than
      // rejecting keeps the contract: a caller in a repaint path must never handle an exception.
      resolve({ stdout: "", error: err instanceof Error ? err.message : String(err) });
    }
  });
}
