/** What you can DO with a character, decided from what that character actually is.
 *
 *  Every action in the panel used to be global: stop, message, show events, reached from a tree
 *  context menu and offered identically whether the run was live or had finished a day ago.
 *  Clicking a character in the world offered nothing at all.
 *
 *  Two rules, and both are about not lying:
 *
 *   - an action is offered only when it would WORK. Stopping a finished agent is not an action,
 *     it is a dead menu item, and one of those teaches you the whole menu is untrustworthy;
 *   - one of your own editor sessions can be READ but not driven. interact does not supervise it,
 *     so offering "message" would promise a reach the product does not have.
 *
 *  No vscode import: the decision is testable, and the caller executes the command.
 */

export interface AgentAction {
  id: string;
  /** What it does, in the words someone watching would use. */
  label: string;
  /** The extension command that performs it. Checked by test to be one of ours. */
  command: string;
  /** A single glyph, so a character can carry its actions without a paragraph of text. */
  mark: string;
}

export interface ActionSubject {
  run_id: string;
  status: string;
  /** Present only when this run IS a definition — a plain claude run has no prompt to open. */
  definition_path?: string | null;
  /** The DEFINITION this run is. A model preference is stored per definition, not per run: you're
   *  choosing what researcher runs on, not what this one errand runs on. */
  agent?: string | null;
  faculties?: string[];
  brain?: boolean;
}

/** Every action, in the order a character offers them. Declared as data so the order is stable —
 *  a menu that reshuffles between renders is one you have to re-read every time. */
const ALL: (AgentAction & { when: (s: ActionSubject) => boolean })[] = [
  {
    id: "message", label: "Say something to them", mark: "✉",
    command: "interact.agents.send",
    // Ours and alive. A finished run has no session left to answer, and a foreign one is not
    // ours to drive.
    when: (s) => s.status === "running",
  },
  {
    id: "transcript", label: "Read what they did", mark: "▤",
    command: "interact.agents.show",
    // Always. This is the transparency the whole product exists for, and it is as true of a run
    // that ended yesterday as of one working now.
    when: () => true,
  },
  {
    id: "prompt", label: "See their instructions", mark: "◱",
    command: "interact.agents.definition",
    when: (s) => Boolean(s.definition_path),
  },
  {
    // "find a way that we could easily chose what models are ran for what" — reachable from the row
    // that IS the agent, rather than only from a settings page somewhere else.
    id: "model", label: "Choose the model it runs on", mark: "◈",
    command: "interact.agents.model",
    // Only where there is an agent to key the choice by: the preference is stored per DEFINITION,
    // so a run with no definition has nothing to remember it against.
    when: (s) => Boolean(s.agent) && s.status !== "foreign",
  },
  {
    id: "events", label: "Raw stream", mark: "⋮",
    command: "interact.agents.showEvents",
    when: (s) => s.status !== "foreign",
  },
  {
    id: "stop", label: "Stop them", mark: "■",
    command: "interact.agents.stop",
    when: (s) => s.status === "running",
  },
];

/** The actions this character offers, filtered to the ones that would actually work. */
export function actionsFor(subject: ActionSubject): AgentAction[] {
  const ours = subject.status !== "foreign";
  return ALL
    .filter((a) => a.when(subject))
    // A foreign session is readable and nothing more: it is one of your own windows, and every
    // driving action on it would be a promise the product cannot keep.
    .filter((a) => ours || a.id === "transcript")
    .map(({ id, label, command, mark }) => ({ id, label, command, mark }));
}
