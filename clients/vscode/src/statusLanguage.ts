/** How a worker's status is SPOKEN — one vocabulary, every surface.
 *
 *  visual-critic, twice: the rail and the workplace carry the same taxonomy in two unrelated
 *  visual languages — the rail says a green ✓ and lowercase "finished" in a flat list row, the
 *  world stamps a tilted uppercase "DONE" on paper. Same five states, two products. Its verdict
 *  was that this split "undercuts the 'one game' read the moment a user sees both panels
 *  together," and that it is very likely part of what Alan called "the environment is weird."
 *
 *  So the vocabulary lives here once and both surfaces render it in their own idiom: the world
 *  stamps the WORD on paper, the rail prints the same word beside the same MARK in the same
 *  ACCENT. Idiom may differ; the words, marks and colours may not.
 *
 *  mark carries the meaning by SHAPE before colour — colour alone is unreadable to a good share
 *  of people and invisible in a screenshot glanced at across a desk.
 */

export type Attention = "error" | "asked" | "held" | "finished" | "working" | "not-ours" | "ready" | "stopped";

export type StatusVoice = {
  /** Shape first: readable without colour. */
  mark: string;
  /** Stamped in the world, printed in the rail. One word, upper case at the render site. */
  word: string;
  /** What the rail says in prose, so a roster reads as sentences rather than jargon. */
  phrase: string;
  /** The theme variable both surfaces tint with — never a hard-coded hex. */
  accent: string;
  /** A state that needs no WORDS. "Agents keep saying 'finished'. Instead, we should not have
   *  that" — a finished agent already sits at rest in the world and carries a quiet ✓ in the
   *  roster; captioning the obvious is noise. Quiet states render their mark only; ERROR and
   *  ASKED keep their words, because those are the two that need him. */
  quiet?: boolean;
  /** Whether the accent is actually PAINTED, or the state renders in neutral ink.
   *
   *  Not every state earns colour. The world stamps HELD and NOT-OURS in plain ink on purpose —
   *  a paused agent and someone else's project are not alarms, and colouring them competes with
   *  the states that do need the eye. The rail was tinting them anyway, so the two surfaces
   *  disagreed about the same worker: visual-critic measured the workplace's HELD stamp at a cool
   *  grey while the rail's pill was warm yellow. The decision belongs HERE, once, or it drifts
   *  again the moment one surface is restyled. */
  tinted: boolean;
};

export const STATUS: Record<Attention, StatusVoice> = {
  error: { mark: "✕", word: "ERROR", phrase: "stopped with an error", accent: "--vscode-charts-red", tinted: true },
  asked: { mark: "✉", word: "ASKED", phrase: "waiting on you", accent: "--vscode-charts-blue", tinted: true },
  held: { mark: "⏸", word: "HELD", phrase: "paused", accent: "--vscode-charts-yellow", tinted: false },
  finished: { mark: "✓", word: "DONE", phrase: "finished", accent: "--vscode-charts-green", tinted: true, quiet: true },
  working: { mark: "●", word: "WORKING", phrase: "working", accent: "--vscode-charts-blue", tinted: true, quiet: true },
  "not-ours": { mark: "○", word: "NOT OURS", phrase: "another project", accent: "--vscode-charts-purple", tinted: false },
  /** Somebody KILLED this run. Not an alarm, but never success: folding it into "finished" hid
   *  exactly the runs a person stopped for a reason. Neutral ink, says its word. */
  stopped: { mark: "■", word: "STOPPED", phrase: "stopped", accent: "--vscode-descriptionForeground", tinted: false },
  /** Declared in the company file, never yet asked for anything. Present, named, quiet — "some
   *  other agents exist but aren't used" was the whole roster rendering as absence. */
  ready: { mark: "·", word: "READY", phrase: "ready", accent: "--vscode-descriptionForeground", tinted: false, quiet: true },
};

/** The voice for a state, falling back to working rather than rendering a blank cell — an
 *  unknown state should look like a person at a desk, never like a hole in the roster. */
export function voiceOf(attention: string): StatusVoice {
  return STATUS[attention as Attention] ?? STATUS.working;
}

/** How long a worker may sit idle before it counts as HELD rather than working.
 *
 *  The same 120 lived in rail.ts as HELD_SECONDS and in the workplace as STALL_SECONDS — one
 *  rule, declared twice, so the roster and the world could disagree about whether the same
 *  person is stuck. The vocabulary owns the words and the colours; it owns the threshold that
 *  decides WHEN a state applies for exactly the same reason.
 */
export const HELD_AFTER_SECONDS = 120;
