/** Acceptance for resolving the packaged conversation bridge without a network bootstrap. */
import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import * as path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
import { restoreEnvironmentAfter } from "../test/fixtures/environment.ts";

const require_ = createRequire(import.meta.url);

test("conversation backend resolution is explicit, local, version-bound, and spawn-free on failure", async () => {
  const backend = require_("../out/conversationBackend.js") as {
    resolveConversationBackend(options: {
      projectPath?: string;
      extensionVersion: string;
      workspaceRoot: string;
      path?: string;
      platform?: NodeJS.Platform;
      pathExt?: string;
    }): Promise<{ available: boolean; command?: string; args?: string[]; reason?: string }>;
  };
  const root = path.resolve("..", "out/tests/conversation-backend");
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(root, { recursive: true });

  const fixture = path.resolve("test/fixtures/conversationFakeHost.ts");
  const stale = path.join(root, "candidate-a");
  const compatible = path.join(root, "candidate-b");
  const unverifiable = path.join(root, "unverifiable-bin");
  const nonExecutable = path.join(root, "non-executable-bin");
  const nonRegular = path.join(root, "non-regular-bin");
  for (const directory of [compatible, stale, unverifiable, nonExecutable, nonRegular]) {
    fs.mkdirSync(directory, { recursive: true });
    const executable = path.join(directory, "interact");
    fs.rmSync(executable, { force: true });
    fs.copyFileSync(fixture, executable);
    fs.chmodSync(executable, 0o700);
  }
  fs.chmodSync(path.join(nonExecutable, "interact"), 0o600);
  fs.writeFileSync(`${path.join(stale, "interact")}.version`, "0.29.0\n");
  fs.writeFileSync(`${path.join(compatible, "interact")}.version`, "0.39.0\n");
  fs.writeFileSync(`${path.join(unverifiable, "interact")}.version`, "not-semver\n");
  fs.rmSync(path.join(nonRegular, "interact"), { force: true });
  fs.mkdirSync(path.join(nonRegular, "interact"));

  const canonicalProject = fs.realpathSync(path.resolve(".."));
  const projectAlias = path.join(root, "project-alias");
  fs.rmSync(projectAlias, { force: true });
  fs.symlinkSync(canonicalProject, projectAlias, "dir");
  const workspaceAlias = path.join(root, "workspace-alias");
  fs.rmSync(workspaceAlias, { force: true });
  fs.symlinkSync(root, workspaceAlias, "dir");

  const dev = await backend.resolveConversationBackend({
    projectPath: projectAlias, extensionVersion: "0.39.0", workspaceRoot: workspaceAlias,
  });
  assert.deepEqual(dev, {
    available: true,
    command: "uv",
    origin: "project_checkout",
    args: ["run", "--directory", canonicalProject, "interact", "agents", "console",
      "--workspace-root", fs.realpathSync(root)],
  });

  const local = await backend.resolveConversationBackend({
    extensionVersion: "0.39.0", workspaceRoot: root, path: compatible,
  });
  assert.equal(local.available, true);
  if (local.available) {
    assert.equal(local.origin, "local_path");
    assert.equal(local.command, path.join(compatible, "interact"));
    assert.deepEqual(local.args, ["agents", "console", "--workspace-root", root]);
    assert.doesNotMatch(`${local.command} ${local.args.join(" ")}`, /uvx|https?:|git\+|\b(?:sh|bash|cmd)\b/);
    const { createConversationClient } = require_("../out/conversationClient.js");
    const client = createConversationClient({ ...local, cwd: root });
    try {
      const catalog = await client.catalog();
      assert.equal(catalog.version, 1);
      assert.ok(Array.isArray(catalog.routes));
      assert.ok(Array.isArray(catalog.criteria));
    } finally { client.dispose(); }
  }

  for (const [name, pathValue, reason] of [
    ["missing", "", /install.*0\.39\.0/i],
    ["non-executable", nonExecutable, /install.*0\.39\.0/i],
    ["non-regular", nonRegular, /install.*0\.39\.0/i],
    ["relative-path-entry", path.relative(process.cwd(), compatible), /install.*0\.39\.0/i],
    ["malformed-version-datum", unverifiable, /could not be verified.*0\.39\.0/i],
  ] as const) {
    const resolved = await backend.resolveConversationBackend({
      extensionVersion: "0.39.0", workspaceRoot: root, path: pathValue,
    });
    assert.equal(resolved.available, false, name);
    assert.match(resolved.reason ?? "", reason, name);
    assert.equal(resolved.command, undefined, `${name} must be unavailable before spawn`);
  }

  const onlyStale = await backend.resolveConversationBackend({
    extensionVersion: "0.39.0", workspaceRoot: root, path: stale,
  });
  assert.equal(onlyStale.available, false, "version identity must come from bounded data, not a path name");
  assert.match(onlyStale.reason ?? "", /0\.29\.0.*0\.39\.0/i);
  const exhaustive = await backend.resolveConversationBackend({
    extensionVersion: "0.39.0", workspaceRoot: root,
    path: [stale, compatible].join(path.delimiter),
  });
  assert.equal(exhaustive.available, true, "a stale first candidate must not hide a later exact version");
  if (exhaustive.available) assert.equal(exhaustive.command, fs.realpathSync(path.join(compatible, "interact")));

  const windows = path.join(root, "windows-bin");
  fs.mkdirSync(windows, { recursive: true });
  const windowsExecutable = path.join(windows, "interact.CMD");
  fs.rmSync(windowsExecutable, { force: true });
  fs.symlinkSync(fixture, windowsExecutable);
  const windowsResolved = await backend.resolveConversationBackend({
    extensionVersion: "0.39.0", workspaceRoot: root,
    path: `${path.join(root, "missing-windows-bin")};${windows}`,
    platform: "win32", pathExt: ".EXE;.CMD",
  });
  assert.equal(windowsResolved.available, true, "the injected platform and PATHEXT define candidate order");
  if (windowsResolved.available) assert.equal(windowsResolved.command, fixture);

  const nonDirectory = path.join(root, "not-a-directory");
  fs.writeFileSync(nonDirectory, "fixture");
  for (const [name, projectPath, workspaceRoot] of [
    ["missing-project", path.join(root, "missing-project"), root],
    ["non-directory-project", nonDirectory, root],
    ["missing-workspace", canonicalProject, path.join(root, "missing-workspace")],
    ["non-directory-workspace", canonicalProject, nonDirectory],
  ] as const) {
    const resolved = await backend.resolveConversationBackend({
      projectPath, extensionVersion: "0.39.0", workspaceRoot,
    });
    assert.equal(resolved.available, false, name);
    assert.match(resolved.reason ?? "", /configured.*directory.*reload/i, name);
  }

  const hadVersionMode = Object.hasOwn(process.env, "INTERACT_FAKE_VERSION_MODE");
  const restoreEnvironment = restoreEnvironmentAfter(["INTERACT_FAKE_VERSION_MODE"]);
  try {
    for (const versionMode of ["malformed", "failure", "timeout"] as const) {
      process.env.INTERACT_FAKE_VERSION_MODE = versionMode;
      const resolved = await backend.resolveConversationBackend({
        extensionVersion: "0.39.0", workspaceRoot: root, path: unverifiable,
      });
      assert.equal(resolved.available, false, versionMode);
      assert.match(resolved.reason ?? "", /could not be verified.*0\.39\.0/i, versionMode);
      assert.equal(resolved.command, undefined, `${versionMode} must fail before host spawn`);
    }
  } finally {
    restoreEnvironment();
  }
  assert.equal(Object.hasOwn(process.env, "INTERACT_FAKE_VERSION_MODE"), hadVersionMode,
    "a test-only mode absent on entry must remain absent after the resolver matrix");
  fs.rmSync(root, { recursive: true });
});

test("the explicit project path reaches the current Python conversation catalog", async () => {
  const backend = require_("../out/conversationBackend.js");
  const { createConversationClient } = require_("../out/conversationClient.js");
  const project = path.resolve("..");
  const workspace = path.join(project, "out", "tests", "conversation-project-path");
  fs.mkdirSync(workspace, { recursive: true });
  const resolved = await backend.resolveConversationBackend({
    projectPath: project, extensionVersion: "0.39.0", workspaceRoot: workspace,
  });
  assert.equal(resolved.available, true);
  const client = createConversationClient({ ...resolved, cwd: workspace });
  try {
    const catalog = await client.catalog();
    assert.equal(catalog.version, 1);
    assert.ok(Array.isArray(catalog.routes));
    assert.ok(Array.isArray(catalog.criteria));
  } finally {
    client.dispose();
  }
});

test("conversation launcher documentation agrees with the local backend boundary", () => {
  const chatSource = fs.readFileSync(path.resolve("src/chatView.ts"), "utf8");
  const clientSource = fs.readFileSync(path.resolve("src/conversationClient.ts"), "utf8");
  const loader = chatSource.match(/\/\*\*(?:(?!\*\/)[\s\S])*\*\/\s*private async loadConversationConsole\(/)?.[0] ?? "";
  const testSource = fs.readFileSync(new URL(import.meta.url), "utf8");

  assert.match(chatSource, /import \{[^}]*resolveConversationBackend[^}]*\} from "\.\/conversationBackend"/);
  assert.match(chatSource, /await resolveConversationBackend\(/);
  assert.doesNotMatch(loader, /\bMCP\b|\buvx\b/i,
    "the active launcher documentation must describe its local backend, not the removed network resolver");
  assert.doesNotMatch(chatSource, /conversationHostArgs/,
    "the active ChatView must not advertise or call the retired argument-rewriting helper");
  assert.doesNotMatch(clientSource, /export function conversationHostArgs\(/,
    "the historical argument rewriter must not remain part of the public client surface");
  assert.doesNotMatch(testSource, /#!\/usr\/bin\/env python|\bimport json, sys\b/,
    "resolver tests must use the shared executable fixture instead of embedded Python programs");
});
