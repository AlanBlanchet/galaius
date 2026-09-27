/** The clock that tells a view whether anyone is still working.
 *
 *  `AgentActivity.at` is declared and documented as "what tells a view that an agent is working
 *  rather than stopped" — but `readAgentActivity` never copied it out of the raw line. So
 *  `lastObservedAt` always returned null, every run's idle time computed as 0, and HELD was
 *  structurally unreachable on BOTH the rail and the workplace: the state exists, is styled, is
 *  in the shared vocabulary, and could never once be shown.
 *
 *  Found by executing the compiled reader against a real registry file rather than by clicking —
 *  no amount of UI testing reaches a state the data layer cannot produce.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createRequire } from "node:module";
import { existsSync } from "node:fs";

/* Loaded from the COMPILED output, not the source. `agents.ts` imports a sibling extensionless,
   which the --experimental-strip-types loader cannot resolve — which is precisely why this module
   had no unit test and why a dropped field survived in it. The critic reached the bug the same
   way: by executing the built code. */
const require_ = createRequire(import.meta.url);
const BUILT = new URL("../out/agents.js", import.meta.url).pathname;
const TEAM = new URL("../out/teamState.js", import.meta.url).pathname;

function seed(lines: object[]): string {
  const home = mkdtempSync(join(tmpdir(), "agents-"));
  const dir = join(home, ".interact", "out", "agents");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "r1.jsonl"), lines.map((l) => JSON.stringify(l)).join("\n"));
  process.env.HOME = home;
  process.env.INTERACT_DEBUG_DIR = join(home, ".interact", "out");
  return home;
}

function loaded(): { readAgentActivity: Function; lastObservedAt: Function } | null {
  if (!existsSync(BUILT) || !existsSync(TEAM)) return null;
  // Fresh each time: these modules resolve paths from HOME at call time, and the seed moves HOME.
  delete require_.cache[require_.resolve(BUILT)];
  delete require_.cache[require_.resolve(TEAM)];
  return {
    readAgentActivity: require_(BUILT).readAgentActivity,
    lastObservedAt: require_(TEAM).lastObservedAt,
  };
}

test("the moment a line was observed survives the read", (t) => {
  const mod = loaded();
  if (!mod) return t.skip("run `npm run compile` first — this reads the built module");
  const { readAgentActivity } = mod;
  seed([
    { kind: "text", text: "starting", at: 1000 },
    { kind: "tool", text: "ran something", tool: "Bash", at: 1200 },
  ]);
  const activity = readAgentActivity("r1", 40);
  assert.equal(activity.length, 2, "the stream did not load at all");
  assert.deepEqual(activity.map((a) => a.at), [1000, 1200],
    "`at` was dropped — idle time then computes as 0 forever and HELD can never be reached");
});

test("and the view can therefore tell when anything last happened", (t) => {
  const mod = loaded();
  if (!mod) return t.skip("needs a build");
  const { readAgentActivity, lastObservedAt } = mod;
  seed([{ kind: "text", text: "a", at: 500 }, { kind: "text", text: "b", at: 900 }]);
  assert.equal(lastObservedAt(readAgentActivity("r1", 40)), 900);
});

test("a stream written before stamping existed still reads, it just has no clock", (t) => {
  const mod = loaded();
  if (!mod) return t.skip("needs a build");
  const { readAgentActivity, lastObservedAt } = mod;
  seed([{ kind: "text", text: "old" }]);
  assert.equal(lastObservedAt(readAgentActivity("r1", 40)), null,
    "absent is absent — it must not be invented as 'now', which would read as freshly active");
});
