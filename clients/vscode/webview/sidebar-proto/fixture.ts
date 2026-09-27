/** Team pulled from the building's fixture, not invented.
 *
 *  Inventing ids here used to invent colours too: pod hue = hash(run_id) resolved against the
 *  other leads on screen. Hue is the only thing tying a report to its lead across panels, so
 *  different invented ids meant the two panels disagreed about who was on whose team.
 *
 *  Fix: one fixture, lives with workplace. This file SELECTS the panel's hard cases from it, plus
 *  the one case the building lacks.
 *
 *  Selected: a lead with 3 reports (one out at web); a second lead with the SAME NAME in another
 *  project (colour is the only difference); a finished report; a dead one; one untouched 2+ min; a
 *  lone lead; a finished lead; a session interact never started.
 *
 *  Every building lead stays even with no reports selected: the hue allocator is greedy, resolving
 *  clashes against the WHOLE lead set — dropping one can shift another's colour. Same lead set =
 *  same colours as the building.
 */
import type { Worker } from "../../src/team";
import { fixture as building } from "../workplace/dev/fixture";

/** Worker (workplace's view model) has no clock — a building shows WHERE, not how long. A board
 *  needs how long, so Docket adds the two timestamps the registry already writes
 *  (AgentRun.started_at / finished_at) that workplace never asked for. Wiring for real: two
 *  fields in TeamState, not a new writer.
 */
export interface Docket extends Worker {
  /** Epoch seconds. Absent on a record written before it was tracked. */
  started_at?: number;
  /** Epoch seconds. Null means still going OR died before the registry stamped it — either way
   *  elapsed shows an open end, never assumes "now". */
  finished_at?: number | null;
}

export interface Board {
  dockets: Docket[];
  /** Wall clock of the snapshot — elapsed computes against a fixed instant, so two screenshots
   *  are byte-identical. */
  at: number;
}

/** Per run: seconds-ago it started and ended (null = still going or unstamped-dead, as above).
 *  Only invented data in this file — the field exists in the registry but stops at workplace's
 *  view model.
 *
 *  Ordered as the board reads: lead, its reports, next lead, its reports.
 */
const CLOCK: [runId: string, ago: number, ended: number | null][] = [
  ["run-a", 2_460, null], // main · interact — the session driving this work
  ["run-b", 1_320, null], //   artist · studio
  ["run-c", 372, null], //     researcher · out at the web
  ["run-f", 3_910, 3_480], //  Explore · finished, and pressed down the spike

  ["run-h", 4_355, null], // main · any-compute — same name, different project, different colour
  ["run-j", 5_200, null], //   generalizer · nobody has been back to it, so it is HELD
  ["run-k", 2_040, 726], //    perf-critic · died, so its slip is torn

  ["run-l", 8_050, null], // visual-critic · a lead on its own
  ["run-o", 1_500, 44], //  optimizer · a lead that has finished
  ["run-n", 5_600, null], // codex · somebody else's session
];

export function fixture(at = Date.UTC(2026, 7, 18, 14, 3, 22)): Board {
  const now = Math.floor(at / 1000);
  const people = new Map(building(at).workers.map((w) => [w.run_id, w]));
  const dockets: Docket[] = [];
  for (const [runId, ago, ended] of CLOCK) {
    const w = people.get(runId);
    // A clock row naming a run the building lacks is a stale fixture — skip it.
    if (!w) continue;
    dockets.push({ ...w, started_at: now - ago, finished_at: ended === null ? null : now - ended });
  }
  return { at, dockets };
}

/** The panel's usual state: ONE session, one report out.
 *
 *  Own fixture because it's the case that judges the panel's bottom half — a full board's pile
 *  reaches the composer, leaving nothing below to judge. A quiet board leaves two inches of paper
 *  and a lot of empty panel below it, which has to be as deliberate as the busy part. It wasn't —
 *  that's what the desk is for.
 */
export function quietBoard(at = Date.UTC(2026, 7, 18, 14, 3, 22)): Board {
  const live = new Set(["run-a", "run-b"]);
  return { at, dockets: fixture(at).dockets.filter((d) => live.has(d.run_id)) };
}

/** The other state a panel has to survive: nothing running at all. */
export function emptyBoard(at = Date.UTC(2026, 7, 18, 14, 3, 22)): Board {
  return { dockets: [], at };
}
