import assert from "node:assert/strict";
import { test, mock } from "node:test";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { acceptWorkspace } from "./workspaceState.ts";
import { parseToolSettings, refreshToolSettings, saveToolSetting, stripPortableEnvironment, toolSettingsView } from "./toolSettings.ts";

const view = { configured: true, revision: 3, account_id: "00000000-0000-0000-0000-000000000001", stale: false,
  portable_keys: ["INTERACT_IMAGE_MODEL", "INTERACT_VIDEO_FPS"], values: { INTERACT_VIDEO_FPS: "12" } };

test("late refresh cannot replace a newer response or clear it on failure", async () => {
  const callbacks: ((error: Error | null, stdout: string, stderr: string) => void)[] = [];
  const patched = mock.method(childProcess, "execFile", (...args: unknown[]) => {
    callbacks.push(args.at(-1) as typeof callbacks[number]);
    return {} as childProcess.ChildProcess;
  });
  syncBuiltinESMExports();
  acceptWorkspace(null, true);
  try {
    for (const fail of [false, true]) {
      const old = refreshToolSettings();
      const fresh = refreshToolSettings();
      const [oldCallback, freshCallback] = callbacks.splice(0);
      freshCallback(null, JSON.stringify({ ok: true, ...view, revision: 5 }), "");
      await fresh;
      oldCallback(fail ? new Error("offline") : null, JSON.stringify({ ok: true, ...view, revision: 4 }), "");
      await old.catch(() => {});
      assert.equal(toolSettingsView()?.revision, 5);
    }
    const sequential = refreshToolSettings();
    callbacks.shift()!(null, JSON.stringify({ ok: true, ...view, revision: 4 }), "");
    assert.equal((await sequential)?.revision, 5, "revision cannot fall even for the newest request");
  } finally {
    patched.mock.restore();
    syncBuiltinESMExports();
    acceptWorkspace(null, false);
  }
});

test("server settings remove old launcher pins while retaining local grants and paths", () => {
  const env = { INTERACT_IMAGE_MODEL: "old", INTERACT_VIDEO_FPS: "5", OPENAI_API_KEY: "fixture", INTERACT_MEDIA_BILLING: "session_only", INTERACT_DEBUG_DIR: "local-path" };
  stripPortableEnvironment(env, parseToolSettings(view));
  assert.deepEqual(env, { OPENAI_API_KEY: "fixture", INTERACT_MEDIA_BILLING: "session_only", INTERACT_DEBUG_DIR: "local-path" });
});

for (const change of [{ revision: -1 }, { account_id: "invalid" }, { stale: "false" }, { values: { OPENAI_API_KEY: "fixture" } }, { values: { INTERACT_VIDEO_FPS: 12 } }]) {
  test(`invalid CLI settings boundary ${JSON.stringify(change)}`, () => assert.throws(() => parseToolSettings({ ...view, ...change })));
}

test("standalone launch settings remain local; server staleness stays explicit", () => {
  const env = { INTERACT_IMAGE_MODEL: "local" };
  stripPortableEnvironment(env, null);
  assert.equal(env.INTERACT_IMAGE_MODEL, "local");
  assert.equal(parseToolSettings({ ...view, stale: true }).stale, true);
});


test("editor save keeps the displayed account/revision and literal argument; refusal retains base", async () => {
  const calls: unknown[][] = [];
  let refused = false;
  const patched = mock.method(childProcess, "execFile", (...args: unknown[]) => {
    calls.push(args);
    const callback = args.at(-1) as (error: Error | null, stdout: string, stderr: string) => void;
    callback(refused ? new Error("fixture conflict") : null,
      JSON.stringify(refused ? { ok: false, message: "Changed on another client; draft retained." } : { ok: true, ...view, revision: 4 }), "");
    return {} as childProcess.ChildProcess;
  });
  syncBuiltinESMExports();
  acceptWorkspace(null, true);
  const base = parseToolSettings(view);
  try {
    await saveToolSetting("INTERACT_IMAGE_MODEL", "literal ; $(no-shell)", base);
    assert.equal(calls[0][0], "interact");
    assert.deepEqual(calls[0][1], ["config", "set", "INTERACT_IMAGE_MODEL", "literal ; $(no-shell)",
      "--expected-revision", "3", "--account-id", view.account_id, "--json-out"]);
    refused = true;
    await assert.rejects(saveToolSetting("INTERACT_IMAGE_MODEL", "corrected", base), /draft retained/);
    assert.equal(base.revision, 3);
    await assert.rejects(saveToolSetting("INTERACT_IMAGE_MODEL", "corrected", { ...base, stale: true }), /stale/);
    assert.equal(calls.length, 2, "stale cache cannot send a write");
  } finally {
    patched.mock.restore();
    syncBuiltinESMExports();
    acceptWorkspace(null, false);
  }
});
