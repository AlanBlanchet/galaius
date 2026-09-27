/** The one duplicated copy of DIM_FOREGROUND must not drift.
 *
 *  `sequenceFormat.ts` cannot import it: the test runner loads that module directly and needs a
 *  `.ts` specifier on every import, which tsc refuses when emitting. So the value is spelled twice
 *  on purpose — and a second copy nobody checks is how the pod colours diverged across the two
 *  panels earlier today. This is the check.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { DIM_FOREGROUND } from "./themeTokens.ts";

const here = dirname(fileURLToPath(import.meta.url));

test("the copy in sequenceFormat matches the shared definition", () => {
  const src = readFileSync(join(here, "sequenceFormat.ts"), "utf8");
  const m = /const DIM_FOREGROUND =\s*([\s\S]*?);/.exec(src);
  assert.ok(m, "sequenceFormat no longer declares its own copy — if it can import now, delete this");

  // Rebuild the literal the same way the module does, so formatting differences do not matter.
  const copy = m[1].split("+").map((p) => p.trim().replace(/^"|"$/g, "")).join("");
  assert.equal(copy, DIM_FOREGROUND);
});

test("nothing consumes the raw token any more", () => {
  // The defect this whole change exists to remove: VS Code's Light theme ships
  // descriptionForeground at 4.28:1 on its own canvas, under the AA floor before we touch it.
  const files = ["sequencePanel.ts", "dashboard.ts", "sequenceFormat.ts", "usage.ts",
                 "chatView.ts", "conversationFormat.ts", "workplaceView.ts"];
  const offenders = files.filter((f) =>
    readFileSync(join(here, f), "utf8").includes("var(--vscode-descriptionForeground)"));
  assert.deepEqual(offenders, []);
});
