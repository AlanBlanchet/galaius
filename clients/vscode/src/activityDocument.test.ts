import { strict as assert } from "node:assert";
import { test } from "node:test";

import { ACTIVITY_SCHEME, activityPath, formatActivity, runIdFromPath } from "./activityDocument.ts";

// "Show agent activity" opened the log as an untitled document, which VS Code treats as DIRTY —
// so peeking at what an agent did left an unsaved file the user then has to answer a save prompt
// for. A custom scheme is read-only by construction, so there is nothing to save.

test("the run id survives the round trip through the document path", () => {
  const runId = "2b7642ee-ecdb-425d-82a7-21f9ce1a240e";
  assert.equal(runIdFromPath(activityPath(runId, "reviewer")), runId);
});

test("the agent name is in the path so the tab is identifiable", () => {
  assert.match(activityPath("abc-123", "reviewer"), /reviewer/);
});

test("a name with slashes or spaces cannot break the path", () => {
  const path = activityPath("abc-123", "my agent/v2");
  assert.equal(runIdFromPath(path), "abc-123");
  assert.ok(!path.includes(" "), `path should not carry raw spaces: ${path}`);
});

test("the scheme is ours, so VS Code never offers to save it", () => {
  assert.equal(ACTIVITY_SCHEME, "interact-agent-activity");
});

test("each event renders as an aligned kind plus its detail", () => {
  const out = formatActivity([
    { kind: "tool", tool: "Read", text: "" },
    { kind: "text", tool: undefined, text: "READY" },
  ]);
  assert.deepEqual(out.split("\n"), ["tool        Read", "text        READY"]);
});

test("an agent with nothing recorded says so instead of rendering blank", () => {
  assert.match(formatActivity([]), /no activity/i);
});
