import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { acceptWorkspace, parseWorkspace, parseWorkspaceModel, workspaceView } from "./workspaceState.ts";
import { readOrg } from "./org.ts";
import { adoptLegacyChoices, chooseModel, clearChoice, chosenModels } from "./agentModels.ts";

function record(count = 2) {
  const agents = Array.from({ length: count }, (_, index) => ({ id: randomUUID(), revision: randomUUID(), name: `Agent ${index}`,
    role_key: `agent-${index}`, department: "engineering", reports_to: null as string | null,
    criteria: "price.in >= 0", criteria_weights: "", reasoning: "high", model: null }));
  for (let index = 1; index < agents.length; index++) agents[index].reports_to = agents[index - 1].id;
  return { ok: true, configured: true, origin: "http://127.0.0.1:8767", workspace_id: randomUUID(), state: "current", writable: true,
    graph: { revision: "a".repeat(64), root_agent: { id: agents[0].id, revision: agents[0].revision }, agents } };
}

test("configured org and model choices come from the same server root and revisions", () => {
  const view = parseWorkspace(record());
  acceptWorkspace(view);
  assert.equal(readOrg()?.coordinator.id, "agent-0");
  assert.equal(readOrg()?.agents[1].reports_to, "agent-0");
  assert.deepEqual(chosenModels(), { "agent-0": "price.in >= 0", "agent-1": "price.in >= 0" });
  assert.equal(chooseModel("agent-0", "provider/model"), false);
  assert.equal(clearChoice("agent-0"), false);
  assert.equal(adoptLegacyChoices(), 0);
  acceptWorkspace(null);
  assert.equal(readOrg(), null, "auth/unavailable state never resurrects generated local org");
  assert.equal(workspaceView(), null);
});

test("boundary rejects wrong roots, duplicate identities, credential URLs and invalid models", () => {
  const wrong = record(); wrong.graph.root_agent.revision = randomUUID();
  assert.throws(() => parseWorkspace(wrong));
  const duplicate = record(); duplicate.graph.agents.push(duplicate.graph.agents[0]);
  assert.throws(() => parseWorkspace(duplicate));
  const credentialUrl = new URL("https://example.invalid");
  credentialUrl.username = randomUUID();
  credentialUrl.password = "test-password";
  assert.throws(() => parseWorkspace({ ...record(), origin: credentialUrl.href }));
  assert.throws(() => parseWorkspaceModel({ id: "model", connection: { id: "../escape", revision: randomUUID(), capability: "http" } }));
  const model = { id: "configured/model", connection: { id: randomUUID(), revision: randomUUID(), capability: "http" } };
  assert.deepEqual(parseWorkspaceModel(model), model);
});

test("1000 deep server agents retain identities and reporting links without recursive conversion", () => {
  const value = record(1000);
  const view = parseWorkspace(value);
  acceptWorkspace(view);
  assert.equal(readOrg()?.agents.length, 1000);
  assert.equal(readOrg()?.agents[999].reports_to, "agent-998");
  assert.throws(() => parseWorkspace(record(1001)));
  acceptWorkspace(null, false);
});
