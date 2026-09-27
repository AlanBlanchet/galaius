/** Where interact's editor-tab surfaces open.
 *
 *  Three of them (team, sequence, dashboard) were each opening their OWN new group beside your
 *  code, so two clicks left the window four or five columns wide and visibly squeezed the side
 *  panel — collateral damage from making them all `Beside` to stop the team stealing the group
 *  holding your code. Both extremes are wrong: stealing your editor, and breeding columns.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { panelColumn, BESIDE } from "./panelColumn.ts";

test("the first interact surface opens beside your code, not on top of it", () => {
  assert.equal(panelColumn([]), BESIDE);
  assert.equal(panelColumn([undefined, undefined]), BESIDE);
});

test("the next one joins the column the first is already in", () => {
  // One place for interact's surfaces, so they stack as tabs rather than as columns.
  assert.equal(panelColumn([3]), 3);
  assert.equal(panelColumn([undefined, 3, undefined]), 3);
});

test("when several are open they all agree on the lowest-numbered one", () => {
  // Deterministic, so the answer does not depend on which happened to be created first.
  assert.equal(panelColumn([5, 3, 4]), 3);
});
