import * as path from "path";

import type { AgentRun } from "./agents";

export interface ChatFile {
  label: string;
  path: string;
}

/** The files behind a run: what it IS, and everything it wrote.
 *
 *  The system prompt's location belongs to the PROVIDER — Claude Code keeps definitions in
 *  ~/.claude/agents/<name>.md, another CLI will not — so Python resolves it through the provider
 *  and records it on the run, and records written before that field existed are repaired on read.
 *  Rebuilding the path here would hard-code one vendor's layout into a panel meant to show runs
 *  from several, so a non-Claude agent would get a link to a file that doesn't exist, and
 *  therefore no link at all.
 *
 *  Only files that exist are offered: a button that opens nothing is worse than no button.
 */
export function chatFiles(
  run: AgentRun,
  dir: string,
  exists: (p: string) => boolean,
): ChatFile[] {
  const definition = run.definition_path ?? null;
  const candidates: ChatFile[] = [
    ...(definition ? [{ label: "system prompt", path: definition }] : []),
    { label: "transcript", path: path.join(dir, `${run.run_id}.jsonl`) },
    { label: "raw stream", path: path.join(dir, `${run.run_id}.raw.jsonl`) },
    { label: "messages", path: path.join(dir, `${run.run_id}.messages.jsonl`) },
  ];
  return candidates.filter((f) => exists(f.path));
}
