import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { WorkflowExecution } from "./workflowExecution.ts";
import { WorkspaceCommandError } from "./serverWorkspace.ts";

test("run and retry pin the selected revision and retain one key after response loss", async () => {
  const workflow = { id: randomUUID(), revision: randomUUID(), name: "Fixture workflow" };
  const calls: readonly string[][] = [];
  const sent: string[][] = calls as string[][];
  let lost = true;
  let current = "running";
  let key = "";
  const execution = new WorkflowExecution(workflow, async args => {
    sent.push([...args]);
    key = args[args.indexOf("--idempotency-key") + 1];
    if (args[0] === "workflow-run") {
      assert.equal(args[args.indexOf("--revision") + 1], workflow.revision);
      assert.deepEqual(JSON.parse(args[args.indexOf("--invocation-json") + 1]), { values: { question: "example" } });
      if (lost) { lost = false; throw new WorkspaceCommandError("unknown", "run_uncertain"); }
    }
    return { run: { id: runId, status: current, idempotency_key: key, workflow: { key: { id: workflow.id }, revision: workflow.revision }, updated_at: new Date().toISOString() } };
  });
  const runId = randomUUID();
  execution.values = '{"question":"example"}';
  await assert.rejects(execution.submit(), /unknown/);
  const originalKey = execution.key;
  assert.throws(() => execution.reset(workflow), /existing execution/);
  assert.equal((await execution.refresh())?.status, "running");
  await execution.submit();
  assert.equal(execution.key, originalKey);
  execution.values = '{"question":"changed"}';
  await assert.rejects(execution.submit(), /Inputs differ/);
  assert.equal(sent.filter(args => args[0] === "workflow-run").length, 2);
  current = "succeeded";
  assert.equal((await execution.refresh())?.status, "succeeded");
  execution.reset(workflow);
  assert.equal(execution.key, null);
});

test("a fresh conflict permits reviewing a new revision; retry conflict cannot erase uncertainty", async () => {
  const workflow = { id: randomUUID(), revision: randomUUID(), name: "Fixture" };
  const execution = new WorkflowExecution(workflow, async () => { throw new WorkspaceCommandError("revision changed", "workflow_conflict"); });
  await assert.rejects(execution.submit(), /revision changed/);
  execution.reset({ ...workflow, revision: randomUUID() });
  assert.notEqual(execution.workflow.revision, workflow.revision);
  let first = true;
  const uncertain = new WorkflowExecution(workflow, async () => {
    const code = first ? "run_uncertain" : "workflow_conflict"; first = false;
    throw new WorkspaceCommandError(code, code);
  });
  await assert.rejects(uncertain.submit());
  await assert.rejects(uncertain.submit());
  assert.throws(() => uncertain.reset(workflow), /existing execution/);
});

test("unrelated run responses never become selected workflow status", async () => {
  const workflow = { id: randomUUID(), revision: randomUUID(), name: "Fixture" };
  const execution = new WorkflowExecution(workflow, async () => ({ run: { id: randomUUID(), status: "succeeded", updated_at: new Date().toISOString(),
    idempotency_key: execution.key, workflow: { key: { id: workflow.id }, revision: randomUUID() } } }));
  await assert.rejects(execution.submit(), /does not match/);
  assert.equal(execution.run, null);
});

test("invalid input rejection permits corrected values and explicit new execution with a new key", async () => {
  const workflow = { id: randomUUID(), revision: randomUUID(), name: "Fixture" };
  const keys: string[] = [];
  const execution = new WorkflowExecution(workflow, async args => {
    if (args[0] === "workflow-status") return { run: null };
    const key = args[args.indexOf("--idempotency-key") + 1];
    keys.push(key);
    const invocation = JSON.parse(args[args.indexOf("--invocation-json") + 1]);
    if ("invalid" in invocation.values) throw new WorkspaceCommandError("input rejected", "run_rejected");
    return { run: { id: randomUUID(), status: "running", idempotency_key: key,
      workflow: { key: { id: workflow.id }, revision: workflow.revision }, updated_at: new Date().toISOString() } };
  });
  execution.values = '{"invalid":"example"}';
  await assert.rejects(execution.submit(), /input rejected/);
  assert.equal(await execution.refresh(), null);
  execution.values = '{"question":"corrected"}';
  execution.reset(workflow);
  assert.equal(execution.values, '{"question":"corrected"}');
  assert.equal((await execution.submit()).status, "running");
  assert.notEqual(keys[0], keys[1]);
});

test("missing status and later rejection cannot clear an earlier unknown outcome", async () => {
  const workflow = { id: randomUUID(), revision: randomUUID(), name: "Fixture" };
  const keys: string[] = [];
  const execution = new WorkflowExecution(workflow, async args => {
    if (args[0] === "workflow-status") return { run: null };
    keys.push(args[args.indexOf("--idempotency-key") + 1]);
    const code = keys.length === 1 ? "run_uncertain" : "run_rejected";
    throw new WorkspaceCommandError(code, code);
  });
  await assert.rejects(execution.submit());
  assert.equal(await execution.refresh(), null);
  assert.throws(() => execution.reset(workflow), /existing execution/);
  await assert.rejects(execution.submit());
  assert.equal(keys[0], keys[1]);
  assert.throws(() => execution.reset(workflow), /existing execution/);
});

test("retry of rejected request that loses its response restores uncertainty", async () => {
  const workflow = { id: randomUUID(), revision: randomUUID(), name: "Fixture" };
  let first = true;
  const execution = new WorkflowExecution(workflow, async () => {
    const code = first ? "run_rejected" : "run_uncertain"; first = false;
    throw new WorkspaceCommandError(code, code);
  });
  await assert.rejects(execution.submit());
  assert.equal(execution.settled, true);
  await assert.rejects(execution.submit());
  assert.throws(() => execution.reset(workflow), /existing execution/);
});
