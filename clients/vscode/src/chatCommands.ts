/** The chat panel's control surface.
 *
 *  All three reference tools (Claude Code, Codex, Copilot) expose their capabilities through a
 *  slash menu in the panel itself. interact's panel could only send a string: stopping the agent
 *  you were reading, starting another, switching workspace, opening the team — all of it lived in
 *  a tree context menu or the command palette. That's what "i can't control everything from
 *  there" means.
 *
 *  Declared as DATA so one list drives the menu, the typed-slash completion and the buttons, and
 *  so a test can hold every entry to invoking a command that genuinely exists.
 */
export interface ChatCommand {
  slash: string;
  title: string;
  detail: string;
  /** The extension command this invokes. Checked by test against the manifest. */
  command: string;
  /** True when it acts on the agent currently open, so the UI can grey it out otherwise. */
  needsAgent: boolean;
}

export const CHAT_COMMANDS: ChatCommand[] = [
  { slash: "/stop", title: "Stop this agent", detail: "interrupt the run you are reading",
    command: "interact.agents.stop", needsAgent: true },
  { slash: "/new", title: "Start an agent", detail: "spawn a new run",
    command: "interact.agents.spawn", needsAgent: false },
  { slash: "/agent", title: "Read another agent", detail: "switch this panel to someone else",
    command: "interact.agents.pick", needsAgent: false },
  // Something none of the reference panels can offer, because they drive one agent: this one
  // supervises several, so addressing all of them at once is a real capability rather than
  // parity. Confirmed before it fires — it reaches everyone who is working.
  { slash: "/all", title: "Message every running agent",
    detail: "one brief to the whole team, running agents only",
    command: "interact.agents.broadcast", needsAgent: false },
  { slash: "/team", title: "Open the team", detail: "the workplace, as a building",
    command: "interact.agents.team", needsAgent: false },
  { slash: "/sequence", title: "Open the sequence", detail: "who talked to whom, in order",
    command: "interact.agents.sequence", needsAgent: false },
  { slash: "/workspace", title: "Change workspace", detail: "show agents from another folder",
    command: "interact.agents.workspace", needsAgent: false },
  { slash: "/events", title: "Raw events", detail: "this agent's unparsed stream",
    command: "interact.agents.showEvents", needsAgent: true },
  { slash: "/prompt", title: "Prompt workspace", detail: "browse and edit the local Git prompt source",
    command: "interact.openDashboard", needsAgent: false },
  { slash: "/permissions", title: "Default autonomy",
    detail: "how much agents started here may do alone",
    command: "interact.agents.permissions", needsAgent: false },
  { slash: "/dashboard", title: "Dashboard", detail: "usage, cost and models",
    command: "interact.openDashboard", needsAgent: false },
  { slash: "/logs", title: "Logs", detail: "what the extension itself is doing",
    command: "interact.showLogs", needsAgent: false },
  { slash: "/refresh", title: "Refresh", detail: "re-read the registry now",
    command: "interact.agents.refresh", needsAgent: false },
];

/** Commands matching what has been typed so far, for the menu. Empty unless it starts with "/". */
export function matchCommands(typed: string): ChatCommand[] {
  const text = typed.trimStart();
  if (!text.startsWith("/")) return [];
  const prefix = text.split(/\s/)[0].toLowerCase();
  return CHAT_COMMANDS.filter((c) => c.slash.startsWith(prefix));
}

/** The command this message IS, if the whole message is one.
 *
 *  Only an exact match counts. "/stop and then rerun" is a sentence that happens to begin with a
 *  slash, and treating it as a command would silently discard what the person actually wrote.
 */
export function commandFor(message: string): ChatCommand | undefined {
  const text = message.trim().toLowerCase();
  return CHAT_COMMANDS.find((c) => c.slash === text);
}
