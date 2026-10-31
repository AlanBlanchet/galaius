/** Where the extension's own files live for a node test, computed once: a test names `OUT`, `SRC`
 *  or `outOf(import.meta.url)`, never its own folder depth, so moving a test moves nothing else. */
import * as path from "node:path";
import { fileURLToPath } from "node:url";

export const EXTENSION = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const SRC = path.join(EXTENSION, "src");
export const OUT = path.join(EXTENSION, "out");
export const REPO = path.resolve(EXTENSION, "..", "..");
/** Scratch the tests write under (outside the extension, so packaging never picks it up). */
export const TEST_OUT = path.resolve(EXTENSION, "..", "out", "tests");

/** The compiled twin of a source's folder: `outOf(import.meta.url)` in src/agents/x.test.ts is out/agents. */
export function outOf(url: string): string {
  return path.join(OUT, path.relative(SRC, path.dirname(fileURLToPath(url))));
}
