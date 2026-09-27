/** Making the Agents panel VISIBLE, not just registered.
 *
 *  viewsContainers.secondarySidebar puts the panel on the right, beside Claude Code and Codex.
 *  But VS Code registers a newly-contributed container as visible: false — read straight out of
 *  a real profile's state:
 *
 *   {"id":"workbench.view.extension.interactAgentsSecondary","pinned":true,"visible":false}
 *
 *  So the panel is in exactly the right place and the user still sees nothing, with no icon to
 *  click and no reason to suspect one exists. A container nobody can see is the same as no
 *  container — hence a one-time reveal on first run, plus a command to summon it any time.
 */

/** The key recording that the panel has been shown once, so later windows never re-hijack focus. */
export const REVEALED_KEY = "interact.agentsPanelRevealed";

/** Reading just the slice of vscode.Memento this needs keeps it testable without the API. */
export interface ReadableState {
  get(key: string): unknown;
}

/** The built-in command that reveals our container. One container: engines.vscode requires
 *  ^1.101.0, which is well past the build that added viewsContainers.secondarySidebar, so an
 *  "older VS Code" fallback could never run. */
export const REVEAL_COMMAND = "workbench.view.extension.interactAgentsSecondary";

/** True only the first time — showing it once is helpful, showing it every window is a hijack. */
export function shouldRevealOnce(state: ReadableState): boolean {
  return state.get(REVEALED_KEY) !== true;
}
