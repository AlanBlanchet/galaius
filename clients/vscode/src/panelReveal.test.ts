import { strict as assert } from "node:assert";
import { test } from "node:test";

import { REVEAL_COMMAND, shouldRevealOnce, REVEALED_KEY } from "./panelReveal.ts";

// VS Code registers a newly-contributed secondary-sidebar container as `visible: false`, so the
// panel exists on the right but the user never sees it and has nothing to click. Read straight
// out of the real profile's state.vscdb:
//   {"id":"workbench.view.extension.interactAgentsSecondary","pinned":true,"visible":false}
// A container nobody can see is the same as no container, so the extension reveals it once.

test("reveals the container the manifest puts in the secondary side bar", () => {
  assert.equal(REVEAL_COMMAND, "workbench.view.extension.interactAgentsSecondary");
});

test("reveals on first run", () => {
  assert.equal(shouldRevealOnce(new Map()), true);
});

test("never reveals again once it has", () => {
  assert.equal(shouldRevealOnce(new Map([[REVEALED_KEY, true]])), false);
});
