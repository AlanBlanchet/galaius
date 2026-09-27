import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import { execFileSync } from "node:child_process";
import * as http from "node:http";
import * as path from "node:path";
import { test } from "node:test";
import { resolveConversationBackend } from "./conversationBackend.ts";
import { promptRequest } from "./promptClient.ts";
import { restoreEnvironmentAfter } from "../test/fixtures/environment.ts";

test("the verified project backend owns prompt catalog read and CAS write", async () => {
  const data = path.resolve("../out/tests/20260906-panels-conversation-workspace/prompt-client-data");
  fs.rmSync(data, { recursive: true, force: true });
  const source = path.join(data, "interact", "prompts");
  fs.mkdirSync(source, { recursive: true });
  fs.writeFileSync(path.join(source, "instructions.md"), "first");
  const restore = restoreEnvironmentAfter(["XDG_DATA_HOME", "UV_OFFLINE", "PYTHONDONTWRITEBYTECODE", "GIT_ASKPASS", "BROWSER"]);
  Object.assign(process.env, { XDG_DATA_HOME: data, UV_OFFLINE: "1", PYTHONDONTWRITEBYTECODE: "1",
    GIT_ASKPASS: "/bin/false", BROWSER: "/bin/false" });
  try {
    const backend = await resolveConversationBackend({
      projectPath: path.resolve("..", ".."), extensionVersion: "0.39.0", workspaceRoot: data,
    });
    assert.equal(backend.available, true);
    if (!backend.available) return;
    const catalog = await promptRequest(backend, ["catalog"]);
    assert.deepEqual(JSON.parse(catalog.output).files, ["instructions.md"]);
    const read = JSON.parse((await promptRequest(backend, ["read", "instructions.md"])).output);
    const written = await promptRequest(backend, ["write", "instructions.md", read.digest], "second");
    assert.equal(written.ok, true, written.output);
    assert.equal(fs.readFileSync(path.join(source, "instructions.md"), "utf8"), "second");
    const stale = JSON.parse((await promptRequest(backend, ["read", "instructions.md"])).output);
    fs.writeFileSync(path.join(source, "instructions.md"), "external disk content");
    const conflict = await promptRequest(backend, ["write", "instructions.md", stale.digest], "preserved draft");
    assert.equal(conflict.ok, false);
    assert.match(conflict.output, /"code":"conflict"[\s\S]*changed/i);
    assert.equal(fs.readFileSync(path.join(source, "instructions.md"), "utf8"), "external disk content");
    fs.appendFileSync(path.join(source, "instructions.md"), "dirty");
    const unavailable = await promptRequest(backend, ["sync"]);
    assert.equal(unavailable.ok, false);
    assert.match(unavailable.output, /could not complete.*explicit action/i);
  } finally { restore(); }
});

test("a missing Git author identity is actionable before the prompt source is staged", async () => {
  const root = path.resolve("../out/tests/20260906-panels-conversation-workspace/prompt-identity");
  fs.rmSync(root, { recursive: true, force: true });
  const data = path.join(root, "data");
  const source = path.join(data, "interact", "prompts");
  fs.mkdirSync(source, { recursive: true });
  fs.writeFileSync(path.join(source, "instructions.md"), "initial\n");
  for (const args of [["init", "--initial-branch=main"], ["add", "."],
    ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial"]]) {
    execFileSync("git", ["-C", source, ...args]);
  }
  fs.writeFileSync(path.join(source, "instructions.md"), "operator draft\n");
  const restore = restoreEnvironmentAfter(["XDG_DATA_HOME", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
    "UV_OFFLINE", "PYTHONDONTWRITEBYTECODE"]);
  Object.assign(process.env, { XDG_DATA_HOME: data, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null",
    UV_OFFLINE: "1", PYTHONDONTWRITEBYTECODE: "1" });
  try {
    const backend = await resolveConversationBackend({ projectPath: path.resolve("../.."),
      extensionVersion: "0.39.0", workspaceRoot: root });
    assert.equal(backend.available, true);
    if (!backend.available) return;
    const result = await promptRequest(backend, ["commit", "--message", "operator commit"]);
    assert.equal(result.ok, false);
    assert.match(result.output, /Git author identity|user\.name.*user\.email/i);
    assert.doesNotThrow(() => execFileSync("git", ["-C", source, "diff", "--cached", "--quiet"],
      { stdio: "ignore" }));
    assert.equal(fs.readFileSync(path.join(source, "instructions.md"), "utf8"), "operator draft\n");
    const before = execFileSync("git", ["-C", source, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    execFileSync("git", ["-C", source, "config", "user.name", "Test Operator"]);
    execFileSync("git", ["-C", source, "config", "user.email", "operator@example.invalid"]);
    const committed = await promptRequest(backend, ["commit", "--message", "operator commit"]);
    assert.equal(committed.ok, true, committed.output);
    assert.notEqual(execFileSync("git", ["-C", source, "rev-parse", "HEAD"], { encoding: "utf8" }).trim(), before);
    assert.doesNotThrow(() => execFileSync("git", ["-C", source, "diff", "--cached", "--quiet"],
      { stdio: "ignore" }));
  } finally { restore(); }
});

test("the typed prompt boundary publishes through one local service without exposing token bytes", async () => {
  const root = path.resolve("../out/tests/20260906-panels-conversation-workspace/prompt-publish");
  fs.rmSync(root, { recursive: true, force: true });
  const data = path.join(root, "data");
  const source = path.join(data, "interact", "prompts");
  fs.cpSync(path.resolve("../../tests/fixtures/prompt_source"), source, { recursive: true });
  fs.chmodSync(path.join(source, "hooks", "hook.sh"), 0o755);
  const remote = path.join(root, "remote.git");
  execFileSync("git", ["init", "--bare", remote]);
  for (const args of [["init", "--initial-branch=main"], ["add", "."],
    ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial"],
    ["remote", "add", "origin", remote], ["push", "--set-upstream", "origin", "main"]]) {
    execFileSync("git", ["-C", source, ...args]);
  }
  const token = path.join(root, "publication-token");
  fs.writeFileSync(token, "private-test-token\n", { mode: 0o600 });
  let publications = 0;
  const server = http.createServer((request, response) => {
    const payload = JSON.stringify({ entries: [], cursor: null, server_timestamp: "2026-09-06T12:00:00Z" });
    if (request.method === "POST" && request.url === "/v1/publications") publications += 1;
    response.writeHead(200, { "content-type": "application/json" }); response.end(payload);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const restore = restoreEnvironmentAfter(["XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "INTERACT_PROMPT_CONSUMER_ROOT", "INTERACT_PROMPT_VSCODE_ROOT", "UV_OFFLINE", "PYTHONDONTWRITEBYTECODE"]);
  Object.assign(process.env, { XDG_DATA_HOME: data, XDG_CACHE_HOME: path.join(root, "cache"),
    XDG_STATE_HOME: path.join(root, "state"), INTERACT_PROMPT_CONSUMER_ROOT: path.join(root, "consumers"),
    INTERACT_PROMPT_VSCODE_ROOT: path.join(root, "vscode"), UV_OFFLINE: "1", PYTHONDONTWRITEBYTECODE: "1" });
  try {
    const backend = await resolveConversationBackend({ projectPath: path.resolve("../.."),
      extensionVersion: "0.39.0", workspaceRoot: root });
    assert.equal(backend.available, true);
    if (!backend.available) return;
    const result = await promptRequest(backend, ["publish", "--endpoint", `http://127.0.0.1:${address.port}`,
      "--token-file", token]);
    assert.equal(result.ok, true, result.output);
    assert.equal(publications, 1);
    assert.doesNotMatch(JSON.stringify(result), /private-test-token/);
  } finally { restore(); await new Promise<void>((resolve) => server.close(() => resolve())); }
});
