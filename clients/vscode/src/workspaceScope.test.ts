/** Which agents the panel is looking at.
 *
 *  It used to look at all of them, always: the workplace rendered every run on the machine with no
 *  notion of where you are, and the tree only GROUPED by project, which is not the same as being
 *  able to look at one. Opening a folder and seeing that folder's team is the default anyone would
 *  expect; being able to switch to another one — a team running in some other checkout — is the
 *  thing that makes the panel a supervisor rather than a log.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { projectFor, projectsOf, scopeRuns, scopeChoices, describeScope, type Scope } from "./workspaceScope.ts";
import type { AgentRun } from "./agents.ts";

function run(over: Partial<AgentRun>): AgentRun {
  return { run_id: "r", provider: "claude", name: "a", status: "running", ...over } as AgentRun;
}

test("a directory belongs to the repository above it, matching what Python records", () => {
  const root = mkdtempSync(join(tmpdir(), "ws-"));
  mkdirSync(join(root, "repo", ".git"), { recursive: true });
  mkdirSync(join(root, "repo", "sub", "deep"), { recursive: true });

  assert.equal(projectFor(join(root, "repo", "sub", "deep")), "repo");
});

test("a sub-package inside a repo still files under the repo", () => {
  // The exact case in this codebase: clients/vscode/package.json must not become its own
  // project, or one repo's agents split across two groups.
  const root = mkdtempSync(join(tmpdir(), "ws-"));
  mkdirSync(join(root, "repo", ".git"), { recursive: true });
  mkdirSync(join(root, "repo", "ext"), { recursive: true });
  writeFileSync(join(root, "repo", "ext", "package.json"), "{}");

  assert.equal(projectFor(join(root, "repo", "ext")), "repo");
});

test("a loose package nobody version-controls gets its own name", () => {
  const root = mkdtempSync(join(tmpdir(), "ws-"));
  mkdirSync(join(root, "tool"), { recursive: true });
  writeFileSync(join(root, "tool", "pyproject.toml"), "");

  assert.equal(projectFor(join(root, "tool")), "tool");
});

const RUNS = [
  run({ run_id: "a", project: "galaius", cwd: "/work/galaius" }),
  run({ run_id: "b", project: "galaius", cwd: "/work/galaius/clients/vscode" }),
  run({ run_id: "c", project: "sheets", cwd: "/work/sheets" }),
  run({ run_id: "d", project: "", cwd: "" }),
];

test("the default scope is the folder you have open", () => {
  const seen = scopeRuns(RUNS, { kind: "current" }, "galaius").map((r) => r.run_id);
  assert.deepEqual(seen, ["a", "b"]);
});

test("a parent workspace includes runs from repositories below its folders", () => {
  const root = mkdtempSync(join(tmpdir(), "ws-"));
  const parent = join(root, "workspace");
  mkdirSync(join(parent, "galaius"), { recursive: true });
  mkdirSync(join(parent, "service"), { recursive: true });
  mkdirSync(join(root, "sheets"), { recursive: true });
  const runs = [
    run({ run_id: "public", project: "galaius", cwd: join(parent, "galaius") }),
    run({ run_id: "sibling", project: "service", cwd: join(parent, "service") }),
    run({ run_id: "outside", project: "sheets", cwd: join(root, "sheets") }),
  ];

  assert.deepEqual(
    scopeRuns(runs, { kind: "current" }, "workspace", [parent]).map((r) => r.run_id),
    ["public", "sibling"],
  );
  assert.deepEqual(
    scopeRuns(runs, { kind: "project", name: "galaius" }, "workspace", [parent])
      .map((r) => r.run_id),
    ["public"],
  );
  assert.equal(scopeRuns(runs, { kind: "all" }, "workspace", [parent]).length, 3);
});

test("current scope accepts runs below any multi-root workspace folder", () => {
  const root = mkdtempSync(join(tmpdir(), "ws-"));
  const first = join(root, "first");
  const second = join(root, "second");
  mkdirSync(join(first, "repo"), { recursive: true });
  mkdirSync(join(second, "repo"), { recursive: true });
  const runs = [
    run({ run_id: "first", project: "one", cwd: join(first, "repo") }),
    run({ run_id: "second", project: "two", cwd: join(second, "repo") }),
  ];

  assert.deepEqual(
    scopeRuns(runs, { kind: "current" }, "one", [first, second]).map((r) => r.run_id),
    ["first", "second"],
  );
});

test("and you can look at every workspace at once", () => {
  assert.equal(scopeRuns(RUNS, { kind: "all" }, "galaius").length, 4);
});

test("and you can look at one you are NOT in — agents running in another checkout", () => {
  // His words: "i have agents in the 'sheets' folder elsewhere, and i can't change and see how
  // they work."
  const seen = scopeRuns(RUNS, { kind: "project", name: "sheets" }, "galaius").map((r) => r.run_id);
  assert.deepEqual(seen, ["c"]);
});

test("with no folder open, scoping to 'current' would hide everything — so it shows everything", () => {
  assert.equal(scopeRuns(RUNS, { kind: "current" }, "").length, 4);
});

test("the switcher lists every workspace that actually has agents", () => {
  assert.deepEqual(projectsOf(RUNS), ["galaius", "sheets"]);
});

test("each scope says plainly what it is showing", () => {
  const cases: Array<[Scope, string]> = [
    [{ kind: "current" }, "galaius"],
    [{ kind: "all" }, "all workspaces"],
    [{ kind: "project", name: "sheets" }, "sheets"],
  ];
  for (const [scope, expected] of cases) {
    assert.match(describeScope(scope, "galaius"), new RegExp(expected));
  }
});


test("the switcher offers this folder first, then everything, then the others", () => {
  // Order is the point: the thing you almost always want is one keystroke away, and the workspace
  // you are NOT in is still reachable without leaving the panel.
  const items = scopeChoices(RUNS, "galaius");
  assert.deepEqual(items.map((i) => i.label), ["galaius", "All workspaces", "sheets"]);
  assert.equal(items[0].detail, "this folder");
  assert.deepEqual(items[0].scope, { kind: "current" });
  assert.deepEqual(items[2].scope, { kind: "project", name: "sheets" });
});

test("with no folder open there is nothing to put first", () => {
  const items = scopeChoices(RUNS, "");
  assert.deepEqual(items.map((i) => i.label), ["All workspaces", "galaius", "sheets"]);
});

test("a workspace with agents is offered even when it is the one you are in", () => {
  // Guards the obvious regression: dedupe that drops the current project from the list entirely.
  const items = scopeChoices(RUNS, "sheets");
  assert.deepEqual(items.map((i) => i.label), ["sheets", "All workspaces", "galaius"]);
});
