import { strict as assert } from "node:assert";
import { createRequire } from "node:module";
import * as path from "node:path";
import { test } from "node:test";

const require_ = createRequire(import.meta.url);

test("disconnected dashboard save retains server draft without local writes or refresh", async () => {
  const updates: unknown[] = [], errors: string[] = [];
  const DashboardPanel = freshDashboard({
    ConfigurationTarget: { Global: 1 },
    workspace: { getConfiguration: () => ({ get: () => undefined, update: async (...args: unknown[]) => { updates.push(args); } }) },
    window: { showErrorMessage: (message: string) => { errors.push(message); } },
  });
  const state = require_(path.resolve("out/workspaceState.js"));
  state.acceptWorkspace(null, false);
  const fs = require_("node:fs");
  const exists = fs.existsSync;
  fs.existsSync = () => false;
  const panel = Object.create(DashboardPanel.prototype);
  panel.settingsSnapshot = { configured: true, revision: 3, account_id: "00000000-0000-0000-0000-000000000001",
    stale: false, portable_keys: ["INTERACT_VIDEO_FPS"], values: { INTERACT_VIDEO_FPS: "12" } };
  let refreshes = 0;
  panel.refreshRevision = 7;
  panel.refresh = () => { refreshes++; };
  try {
    await panel.handleMessage({ type: "saveSetting", setting: "video.fps", value: "17" });
    assert.deepEqual(updates, []);
    assert.equal(refreshes, 0);
    assert.equal(panel.refreshRevision, 8, "an in-flight refresh must not overwrite the retained draft");
    assert.match(errors[0], /Reload.*draft retained/i);
    assert.equal(panel.settingsSnapshot.revision, 3);
  } finally {
    fs.existsSync = exists;
    delete require_.cache[require_.resolve(path.resolve("out/dashboard.js"))];
    delete require_.cache[require_.resolve(path.resolve("out/shared.js"))];
  }
});

function freshDashboard(vscode: unknown): any {
  const Module = require_("node:module") as { _load(request: string, parent: unknown, main: boolean): unknown };
  const original = Module._load;
  const dashboard = path.resolve("out/dashboard.js");
  const shared = path.resolve("out/shared.js");
  delete require_.cache[require_.resolve(dashboard)];
  delete require_.cache[require_.resolve(shared)];
  Module._load = (request, parent, main) => request === "vscode" ? vscode
    : request === "./leaderboard" ? { readLeaderboard: () => null } : original(request, parent, main);
  try { return require_(dashboard).DashboardPanel; }
  finally { Module._load = original; }
}

test("Configuration edits typed nonsecret settings in-panel and persists only valid fields", async () => {
  const values = new Map<string, unknown>();
  const updates: [string, unknown][] = [];
  const Module = require_("node:module") as { _load(request: string, parent: unknown, main: boolean): unknown };
  const original = Module._load;
  Module._load = (request, parent, main) => request === "vscode" ? {
    ConfigurationTarget: { Global: 1 },
    workspace: { getConfiguration: () => ({
      get: (key: string) => values.get(key),
      update: async (key: string, value: unknown) => { values.set(key, value); updates.push([key, value]); },
    }) },
    window: {}, commands: {}, Uri: {}, env: {},
  } : request === "./leaderboard" ? { readLeaderboard: () => null } : original(request, parent, main);
  try {
    const { DashboardPanel } = require_(path.resolve("out/dashboard.js"));
    const panel = Object.create(DashboardPanel.prototype) as any;
    panel.modelsData = { providers: {}, recommendations: {} };
    panel.refresh = () => {};
    const rows = panel.settingsCells().flatMap((cell: any) => cell.content);
    assert.ok(rows.some((row: any) => row.kind === "setting" && row.input === "bool"));
    assert.ok(rows.some((row: any) => row.kind === "setting" && row.input === "int"));
    assert.ok(rows.every((row: any) => row.kind !== "row" ||
      !row.actions?.some((action: any) => action.type === "changeSetting")));
    const integer = rows.find((row: any) => row.kind === "setting" && row.input === "int");
    await panel.handleMessage({ type: "saveSetting", setting: integer.key, value: "not-an-integer" });
    assert.deepEqual(updates, []);
    await panel.handleMessage({ type: "saveSetting", setting: integer.key, value: "7" });
    assert.deepEqual(updates, [[integer.key, 7]]);
    await panel.handleMessage({ type: "saveSetting", setting: integer.key, value: "" });
    assert.deepEqual(updates, [[integer.key, 7], [integer.key, undefined]],
      "blank clears the override so the declarative default is read again");
    const reopened = panel.settingsCells().flatMap((cell: any) => cell.content)
      .find((row: any) => row.kind === "setting" && row.key === integer.key);
    assert.equal(reopened.value, String(reopened.defaultValue),
      "reopening reads and renders the declared default after the override is cleared");
    await panel.handleMessage({ type: "saveSetting", setting: integer.key, value: "0" });
    await panel.handleMessage({ type: "saveSetting", setting: integer.key, value: "-2" });
    const width = rows.find((row: any) => row.kind === "setting" && row.key === "browser.viewportWidth");
    await panel.handleMessage({ type: "saveSetting", setting: width.key, value: "0" });
    assert.deepEqual(updates, [[integer.key, 7], [integer.key, undefined], [integer.key, 0], [integer.key, -2]],
      "the schema minimum rejects a zero viewport");
    assert.equal(width.minimum, 1);
    await panel.handleMessage({ type: "saveSetting", setting: width.key, value: "1024" });
    assert.deepEqual(updates.at(-1), [width.key, 1024]);
    const nestedSize = rows.find((row: any) => row.kind === "setting" && row.key === "desktop.nestedSize");
    assert.equal(nestedSize.pattern, "^[1-9]\\d*x[1-9]\\d*$");
    await panel.handleMessage({ type: "saveSetting", setting: nestedSize.key, value: "garbage" });
    assert.equal(updates.some(([key]) => key === nestedSize.key), false);
    await panel.handleMessage({ type: "saveSetting", setting: nestedSize.key, value: "1024x768" });
    assert.deepEqual(updates.at(-1), [nestedSize.key, "1024x768"]);
    await panel.handleMessage({ type: "saveSetting", setting: nestedSize.key, value: "" });
    assert.deepEqual(updates.at(-1), [nestedSize.key, undefined]);
  } finally { Module._load = original; }
});

test("typed settings save and read back serially without crossing fields", async () => {
  const values = new Map<string, unknown>();
  const updates: [string, unknown][] = [];
  const DashboardPanel = freshDashboard({
    ConfigurationTarget: { Global: 1 },
    workspace: { getConfiguration: () => ({
      get: (key: string) => values.get(key),
      update: async (key: string, value: unknown) => { values.set(key, value); updates.push([key, value]); },
    }) }, window: {}, commands: {}, Uri: {}, env: {},
  });
  const panel = Object.create(DashboardPanel.prototype) as any;
  panel.modelsData = { providers: {}, recommendations: {} };
  panel.refresh = () => {};
  const rows = panel.settingsCells().flatMap((cell: any) => cell.content);
  const cases = [
    { kind: "bool", value: "true", stored: true, invalid: "maybe", clears: false },
    { kind: "enum", value: "api", stored: "api", invalid: "not-an-option", clears: false },
    { kind: "int", value: "-2", stored: -2, invalid: "1.5", clears: true },
    { kind: "str", value: "latency<=0.5", stored: "latency<=0.5", clears: true },
    { kind: "path", value: "/workspace/profile", stored: "/workspace/profile", clears: true },
  ];
  for (const item of cases) {
    const row = rows.find((candidate: any) => candidate.kind === "setting" && candidate.input === item.kind
      && (item.kind !== "int" || candidate.minimum == null));
    assert.ok(row, item.kind);
    if (item.invalid) {
      const before = updates.length;
      await panel.handleMessage({ type: "saveSetting", setting: row.key, value: item.invalid });
      assert.equal(updates.length, before);
    }
    await panel.handleMessage({ type: "saveSetting", setting: row.key, value: item.value });
    assert.deepEqual(updates.at(-1), [row.key, item.stored]);
    const reopened = panel.settingsCells().flatMap((cell: any) => cell.content)
      .find((candidate: any) => candidate.kind === "setting" && candidate.key === row.key);
    assert.equal(reopened.value, String(item.stored));
    const beforeClear = updates.length;
    await panel.handleMessage({ type: "saveSetting", setting: row.key, value: "" });
    if (item.clears) assert.deepEqual(updates.at(-1), [row.key, undefined]);
    else assert.equal(updates.length, beforeClear);
  }
});

test("an older deferred refresh cannot post settings after the newest refresh", async () => {
  const values = new Map<string, unknown>([["browser.viewportWidth", 640]]);
  const DashboardPanel = freshDashboard({ workspace: { getConfiguration: () => ({
    get: (key: string) => values.get(key), update: async () => {},
  }) }, window: {}, commands: {}, Uri: {}, env: {} });
  const panel = Object.create(DashboardPanel.prototype) as any;
  const posted: any[] = [];
  const releases: ((value: unknown) => void)[] = [];
  panel.disposed = false;
  panel.refreshRevision = 0;
  panel.promptState = { files: ["instructions.md"] };
  panel.panel = { webview: { postMessage: (message: any) => posted.push(message) } };
  panel.modelsData = { providers: {}, recommendations: {} };
  panel.benchmarksData = { benchmarks: [] };
  panel.conversationCatalog = () => undefined;
  for (const name of ["statusCell", "apiKeysCell", "benchmarkDataCell", "displayCell", "agentsCell",
    "benchmarksCell", "recommendationsCell"]) panel[name] = () => ({ id: name, content: [] });
  panel.modelsCell = () => new Promise((resolve) => releases.push(resolve));
  panel.consumptionCell = async () => ({ id: "consumption", content: [] });
  const older = panel.refresh();
  values.set("browser.viewportWidth", 1024);
  const newer = panel.refresh();
  releases[1]({ id: "models", content: [] });
  await newer;
  releases[0]({ id: "models", content: [] });
  await older;
  const widths = posted.filter((message) => message.cell.id === "cfg-browser")
    .map((message) => message.cell.content.find((row: any) => row.key === "browser.viewportWidth")?.value);
  assert.deepEqual(widths, ["1024"]);
});

test("disposal during a deferred refresh posts no cells", async () => {
  const DashboardPanel = freshDashboard({ workspace: { getConfiguration: () => ({ get: () => undefined }) },
    window: {}, commands: {}, Uri: {}, env: {} });
  const panel = Object.create(DashboardPanel.prototype) as any;
  const posted: unknown[] = [];
  let release!: (value: unknown) => void;
  panel.disposed = false;
  panel.refreshRevision = 0;
  panel.promptState = { files: ["instructions.md"] };
  panel.panel = { webview: { postMessage: (message: unknown) => posted.push(message) } };
  panel.modelsData = { providers: {}, recommendations: {} };
  panel.benchmarksData = { benchmarks: [] };
  panel.conversationCatalog = () => undefined;
  for (const name of ["statusCell", "apiKeysCell", "benchmarkDataCell", "displayCell", "agentsCell",
    "benchmarksCell", "recommendationsCell"]) panel[name] = () => ({ id: name, content: [] });
  panel.modelsCell = () => new Promise((resolve) => { release = resolve; });
  panel.consumptionCell = async () => ({ id: "consumption", content: [] });
  const refresh = panel.refresh();
  panel.disposed = true;
  release({ id: "models", content: [] });
  await refresh;
  assert.deepEqual(posted, []);
});

test("a deferred initial catalog cannot overwrite a newer explicit prompt selection", async () => {
  const DashboardPanel = freshDashboard({ workspace: { getConfiguration: () => ({ get: () => undefined }) },
    window: {}, commands: {}, Uri: {}, env: {} });
  const panel = Object.create(DashboardPanel.prototype) as any;
  const posted: any[] = [];
  let releaseCatalog!: () => void;
  panel.disposed = false;
  panel.refreshRevision = 0;
  panel.promptState = { files: [], content: "", digest: "" };
  panel.panel = { webview: { postMessage: (message: any) => posted.push(message) } };
  panel.modelsData = { providers: {}, recommendations: {} };
  panel.benchmarksData = { benchmarks: [] };
  panel.conversationCatalog = () => undefined;
  for (const name of ["statusCell", "apiKeysCell", "benchmarkDataCell", "displayCell", "agentsCell",
    "benchmarksCell", "recommendationsCell"]) panel[name] = () => ({ id: name, content: [] });
  panel.modelsCell = async () => ({ id: "models", content: [] });
  panel.consumptionCell = async () => ({ id: "consumption", content: [] });
  panel.loadPromptCatalog = async () => {
    await new Promise<void>((resolve) => { releaseCatalog = resolve; });
    return { files: ["old.md"], selected: "old.md", content: "STALE", digest: "old" };
  };
  panel.loadPrompt = async () => {
    panel.promptState = { files: ["new.md"], selected: "new.md", content: "CURRENT", digest: "new" };
  };
  const initial = panel.refresh();
  const explicit = panel.handleMessage({ type: "promptSelect", path: "new.md" });
  await explicit;
  releaseCatalog();
  await initial;
  const prompts = posted.filter((message) => message.cell.id === "prompt-workspace")
    .map((message) => message.cell.content[0].selected);
  assert.deepEqual(prompts, ["new.md"]);
});

test("one dashboard refresh loads and renders exactly one prompt workspace", async () => {
  const Module = require_("node:module") as { _load(request: string, parent: unknown, main: boolean): unknown };
  const original = Module._load;
  Module._load = (request, parent, main) => request === "vscode" ? {
    workspace: { getConfiguration: () => ({ get: () => undefined }) }, window: {}, commands: {}, Uri: {}, env: {},
  } : request === "./leaderboard" ? { readLeaderboard: () => null } : original(request, parent, main);
  try {
    const { DashboardPanel } = require_(path.resolve("out/dashboard.js"));
    const panel = Object.create(DashboardPanel.prototype) as any;
    const cells: any[] = [];
    let loads = 0;
    panel.disposed = false;
    panel.refreshRevision = 0;
    panel.promptState = { files: [], content: "", digest: "" };
    panel.loadPromptCatalog = async () => { loads += 1; return { files: ["instructions.md"] }; };
    panel.panel = { webview: { postMessage: ({ cell }: any) => cells.push(cell) } };
    for (const name of ["statusCell", "apiKeysCell", "benchmarkDataCell", "displayCell", "agentsCell",
      "consumptionCell", "benchmarksCell", "recommendationsCell"]) panel[name] = () => ({ id: name, content: [] });
    panel.settingsCells = () => [];
    panel.modelsCell = async () => ({ id: "models", content: [] });
    panel.modelsData = { providers: {}, recommendations: {} };
    panel.benchmarksData = { benchmarks: [] };
    panel.conversationCatalog = () => undefined;
    await panel.refresh();
    assert.equal(loads, 1);
    assert.equal(cells.filter((cell) => cell.id === "prompt-workspace").length, 1);
  } finally { Module._load = original; }
});

test("Publish validates in-panel service inputs and invokes the canonical prompt CLI boundary", async () => {
  const Module = require_("node:module") as { _load(request: string, parent: unknown, main: boolean): unknown };
  const original = Module._load;
  Module._load = (request, parent, main) => request === "vscode" ? {
    workspace: { getConfiguration: () => ({ get: () => undefined }) }, window: {}, commands: {}, Uri: {}, env: {},
  } : request === "./leaderboard" ? { readLeaderboard: () => null } : original(request, parent, main);
  try {
    const { DashboardPanel } = require_(path.resolve("out/dashboard.js"));
    const panel = Object.create(DashboardPanel.prototype) as any;
    const calls: string[][] = [];
    panel.promptState = { files: ["instructions.md"], selected: "instructions.md", content: "dirty", digest: "d" };
    panel.refresh = () => {};
    panel.promptCall = async (args: string[]) => { calls.push(args); return { ok: true, output: "published" }; };
    await panel.handleMessage({ type: "promptAction", action: "publish", explicit: true,
      endpoint: "javascript:bad", tokenFile: "relative-token" });
    assert.deepEqual(calls, []);
    assert.match(panel.promptState.status, /HTTPS or local HTTP.*absolute token file/i);
    await panel.handleMessage({ type: "promptAction", action: "publish", explicit: true,
      endpoint: "http://127.0.0.1:43123", tokenFile: "/owned/token" });
    assert.deepEqual(calls, [["publish", "--endpoint", "http://127.0.0.1:43123", "--token-file", "/owned/token"]]);
    assert.equal(panel.promptState.content, "dirty");
    assert.equal(panel.promptState.digest, "d");
    panel.promptCall = async (args: string[]) => { calls.push(args); return { ok: false,
      output: "Prompt action could not complete; review the local prompt status and try the explicit action again." }; };
    await panel.handleMessage({ type: "promptAction", action: "publish", explicit: true,
      endpoint: "http://127.0.0.1:43124", tokenFile: "/owned/token" });
    assert.match(panel.promptState.status, /could not complete/);
    assert.equal(panel.promptState.content, "dirty");
    assert.equal(panel.promptState.digest, "d");
  } finally { Module._load = original; }
});

test("a prompt CAS conflict preserves the submitted draft and base digest until explicit reload", async () => {
  const Module = require_("node:module") as { _load(request: string, parent: unknown, main: boolean): unknown };
  const original = Module._load;
  Module._load = (request, parent, main) => request === "vscode" ? {
    workspace: { getConfiguration: () => ({ get: () => undefined }) }, window: {}, commands: {}, Uri: {}, env: {},
  } : request === "./leaderboard" ? { readLeaderboard: () => null } : original(request, parent, main);
  try {
    const { DashboardPanel } = require_(path.resolve("out/dashboard.js"));
    const panel = Object.create(DashboardPanel.prototype) as any;
    panel.promptState = { files: ["instructions.md"], selected: "instructions.md",
      content: "BASE", digest: "base-digest" };
    panel.refresh = () => {};
    panel.promptCall = async () => ({ ok: false, output: JSON.stringify({ ok: false, code: "conflict",
      error: "prompt source changed; preserve the editor buffer and reload" }) });
    await panel.handleMessage({ type: "promptSave", path: "instructions.md",
      content: "UNSAVED_OPERATOR_DRAFT\nsecond line", digest: "base-digest" });
    assert.equal(panel.promptState.content, "UNSAVED_OPERATOR_DRAFT\nsecond line");
    assert.equal(panel.promptState.digest, "base-digest");
    assert.match(panel.promptState.status, /conflict.*draft.*Reload from disk/i);
  } finally { Module._load = original; }
});
