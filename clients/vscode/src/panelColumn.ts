/** Where interact's editor-tab surfaces open.
 *
 *  Two failures bracket this, and the fix for the first caused the second:
 *
 *   - the team used to open in ViewColumn.Active, taking over the editor group holding your
 *     code — so the thing you were reading vanished behind a pixel-art building;
 *   - making all of them Beside fixed that and introduced sprawl: team, sequence and dashboard
 *     each opened their OWN new group, so two clicks left the window four or five columns wide and
 *     visibly squeezed the side panel.
 *
 *  So: the FIRST interact surface goes beside your code, and every later one joins it there,
 *  stacking as tabs in one column instead of breeding columns.
 */

/** VS Code's ViewColumn.Beside is -2. Named here so this module needs no vscode import and
 *  can therefore be unit-tested, like agentsFormat.ts and rail.ts. */
export const BESIDE = -2;

/** The column an interact surface should open in, given the columns its siblings currently hold.
 *
 *  Lowest-numbered wins so the answer is deterministic — otherwise it would depend on which panel
 *  happened to be created first, and the same two clicks in a different order would lay the window
 *  out differently.
 */
export function panelColumn(openColumns: readonly (number | undefined)[]): number {
  const held = openColumns.filter((c): c is number => typeof c === "number");
  return held.length ? Math.min(...held) : BESIDE;
}

/** The columns interact's editor-tab surfaces currently occupy, so a new one joins them rather
 *  than opening yet another group. Registered by each panel; see panelColumn.ts for why. */
const OPEN_COLUMNS = new Map<string, number | undefined>();

export function claimColumn(id: string, column: number | undefined): void {
  OPEN_COLUMNS.set(id, column);
}

export function releaseColumn(id: string): void {
  OPEN_COLUMNS.delete(id);
}

export function nextColumn(): number {
  return panelColumn([...OPEN_COLUMNS.values()]);
}
