/** What each agent can actually DO, inherited from its own definition file.
 *
 *  Every agent definition already declares this in its frontmatter —
 *  tools: [Read, Grep, Bash, mcp__interact__screenshot, …] — and nothing had ever read it. So
 *  the workplace drew every character identically whether it could only read files or could drive
 *  a browser, spawn other agents and spend money. A team where you can't see who can do what is
 *  not a team you can supervise.
 *
 *  Tools are grouped into a few human FACULTIES rather than listed raw. Nobody watching a
 *  workplace wants mcp__interact__get_interactive_elements floating over a sprite; they want to
 *  know that one can SEE and another can only READ. A tool list is inventory; a faculty is what
 *  the character can do.
 *
 *  No vscode import and no filesystem here — the caller supplies the text — so every decision
 *  below is unit-testable, like rail.ts and agentsFormat.ts.
 */

export interface Faculty {
  id: string;
  /** What a person watching would call it. */
  label: string;
  /** A single glyph the world can put over a character's head. Shape carries it, never colour. */
  mark: string;
}

/** Every faculty a character can have. Declared, so the world can draw them all and a test can
 *  hold the mapping to this list rather than to whatever a tool name happened to imply. */
export const FACULTIES: Faculty[] = [
  { id: "reads", label: "reads the code", mark: "◱" },
  { id: "writes", label: "changes files", mark: "✎" },
  { id: "runs", label: "runs commands", mark: "▸" },
  { id: "sees", label: "sees the screen", mark: "◉" },
  { id: "searches", label: "searches the web", mark: "🜁" },
  { id: "delegates", label: "puts others to work", mark: "⚯" },
];

const ORDER = FACULTIES.map((f) => f.id);

/** Which faculty a tool name grants, or null when we don't recognise it.
 *
 *  Unknown grants NOTHING, deliberately. A vendor adding a tool must never silently hand a
 *  character a power it doesn't have — on this axis, guessing high is the dangerous direction.
 */
function facultyOf(tool: string): string | null {
  if (/^(Read|Grep|Glob|NotebookRead)$/.test(tool)) return "reads";
  if (/^(Write|Edit|NotebookEdit)$/.test(tool)) return "writes";
  if (/^(Bash|BashOutput|KillShell)$/.test(tool)) return "runs";
  if (/^(WebSearch|WebFetch)$/.test(tool)) return "searches";
  if (/^(Agent|Task|SendMessage|ListAgents)$/.test(tool)) return "delegates";
  // interact's own surface: eyes and hands on a real screen. report_issue is not a faculty, it's
  // paperwork — grants nothing so it doesn't dress a character up as an operator.
  if (/^mcp__interact__(screenshot|run_actions|get_interactive_elements|get_page_state|record|launch_app|list_desktop_windows|session|navigate|measure_ui|review_ui|verify_ui|transcribe)$/.test(tool)) {
    return "sees";
  }
  return null;
}

/** The tools a definition declares, from its FRONTMATTER only.
 *
 *  Frontmatter only: the body of a definition is prose that routinely mentions tool names and
 *  even example tools: lines — parsing those would grant powers the agent never had.
 */
export function parseCapabilities(definition: string): string[] {
  const fence = definition.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!fence) return [];
  const line = fence[1].match(/^tools:\s*(.+)$/m);
  if (!line) return [];
  return line[1]
    .replace(/^\[|\]$/g, "")
    .split(",")
    .map((t) => t.trim().replace(/^["']|["']$/g, ""))
    .filter(Boolean);
}

/** The faculties a set of tools amounts to, deduped and in a stable order.
 *
 *  Stable because a character reshuffling its own badges between renders reads as flicker, and
 *  because two agents with the same powers should look the same.
 */
export function facultiesOf(tools: readonly string[]): string[] {
  const held = new Set<string>();
  for (const tool of tools) {
    const faculty = facultyOf(tool);
    if (faculty) held.add(faculty);
  }
  return ORDER.filter((id) => held.has(id));
}

/** The faculty records for an agent, ready to render. */
export function facultyMarks(tools: readonly string[]): Faculty[] {
  const held = new Set(facultiesOf(tools));
  return FACULTIES.filter((f) => held.has(f.id));
}
