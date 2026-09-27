/** "on the sidepanel, we should be able to view what TASKS an agent was given, and then proceed to
 *  view the conversation we want etc..."
 *
 *  The sidebar held exactly one depth — a single conversation — so there was nowhere to SEE that an
 *  agent had been given six errands, and no way to pick between them. This is the missing middle:
 *  the agent, its identity, and the tasks it was given, newest first.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { agentView, type AgentIdentity } from "./agentPanel.ts";
import { roleOf } from "./roster.ts";

const who: AgentIdentity = {
  id: "researcher", title: "Researcher", department: "research",
  model: "ollama/deepseek-v4-pro:cloud", declaredModel: "claude-sonnet-5",
  definitionPath: "/tmp/project/agents/researcher.md",
};
const task = (id: string, o: Partial<{ task: string; status: string; started_at: number }> = {}) =>
  ({ run_id: id, provider: "claude", agent: "researcher", task: o.task ?? `errand ${id}`,
     status: o.status ?? "done", started_at: o.started_at ?? 0 });

test("it shows the tasks the agent was given, newest first", () => {
  const html = agentView(who, [task("a", { started_at: 10 }), task("b", { started_at: 900 })], "N");
  const first = html.indexOf("errand b"), second = html.indexOf("errand a");
  assert.ok(first >= 0 && second >= 0, "both errands must be listed");
  assert.ok(first < second, "the newest errand is the one you are most likely to want");
});

test("each task is something you can open", () => {
  const html = agentView(who, [task("a"), task("b")], "N");
  assert.match(html, /data-run="a"/);
  assert.match(html, /data-run="b"/);
});

test("the identity says who this is, not just its id", () => {
  const html = agentView(who, [], "N");
  for (const bit of ["researcher", "Researcher", "research"]) {
    assert.ok(html.includes(bit), `identity is missing ${bit}`);
  }
});

test("the model it runs on is shown, and says when you overrode the company file", () => {
  /* "find a way that we could easily chose what models are ran for what" — and a choice you cannot
     SEE is a choice you will forget you made. */
  const html = agentView(who, [], "N");
  assert.ok(html.includes("deepseek-v4-pro:cloud"), "the model in force is not shown");
  assert.match(html, /data-action="model"/, "and it must be changeable from here");
  assert.ok(/declared|overrid/i.test(html), "an override must read as an override, not as the default");
});

test("when no choice was made, the company file's model is shown plainly", () => {
  const html = agentView({ ...who, model: null }, [], "N");
  assert.ok(html.includes("claude-sonnet-5"));
  assert.ok(!/overrid/i.test(html), "nothing was overridden, so nothing should claim it was");
});

test("the file that DEFINES the agent can be opened from here", () => {
  /* "i can, if i want, easily manage the agent files from the central panel, or directly from the
     agent view" — and `OrgAgent.def` was declared in the company file and read by nothing. */
  const html = agentView(who, [], "N");
  assert.match(html, /data-action="definition"/);
  assert.ok(html.includes("researcher.md"), "show the file's name, not an opaque button");
});

test("an agent with no definition file offers no way to open one", () => {
  const html = agentView({ ...who, definitionPath: null }, [], "N");
  assert.ok(!html.includes('data-action="definition"'), "a dead control teaches the menu is a lie");
});

test("an agent that has been given nothing says so", () => {
  const html = agentView(who, [], "N");
  assert.ok(/no tasks|nothing/i.test(html), "an empty list must read as empty, not as broken");
});

test("everything from the outside is escaped", () => {
  /* Task text is whatever someone typed, and a definition path comes off disk. */
  const html = agentView({ ...who, title: "<img src=x onerror=alert(1)>" },
    [task("a", { task: "</span><script>bad()</script>" })], "N");
  assert.ok(!html.includes("<img"), "identity is not escaped");
  assert.ok(!html.includes("<script>bad"), "task text is not escaped");
});

test("every chip carries the agent it acts on", () => {
  /* The definition chip shipped with `data-path` and no `data-agent`, while the host's handler
     gated on the agent being present before running EITHER chip's action — so the branch never
     ran and the chip was dead. A chip that looks live and does nothing is the exact defect this
     project keeps re-shipping, so the contract is pinned here rather than trusted. */
  const html = agentView(who, [], "N");
  const chips = html.match(/<button class="chip"[^>]*>/g) ?? [];
  assert.ok(chips.length >= 2, `expected the model and definition chips, got ${chips.length}`);
  for (const chip of chips) {
    assert.match(chip, /data-action="/, `a chip with no action: ${chip}`);
    assert.match(chip, /data-agent="researcher"/, `a chip that does not say who it acts on: ${chip}`);
  }
});

test("an agent's tasks are found by resolved identity, not by a raw field", () => {
  /* The blocking defect: clicking the coordinator showed "No tasks yet" while five of its errands
     were visibly failing in the same room. A definition-less run's `agent` is null — its id
     ("main") exists only through roleOf's fallback — so filtering on the literal field found
     nothing for exactly the case the change was written for. Pinned as a rule about WHICH runs
     belong to an agent, since the renderer takes them already filtered. */
  const bare = [
    { run_id: "c1", provider: "claude", name: "claude", agent: null, task: "one", status: "failed" },
    { run_id: "c2", provider: "claude", name: "claude", agent: null, task: "two", status: "done" },
  ];
  const company = { coordinator: { id: "main", title: "Main thread" }, binaries: ["claude"] };
  const mine = bare.filter((r) => roleOf(r as never, company).id === "main");
  assert.equal(mine.length, 2, "the coordinator's own errands must resolve to it");

  const html = agentView({ id: "main", title: "Main thread" }, mine as never[], "N");
  assert.ok(html.includes("one") && html.includes("two"));
  assert.ok(!/no tasks/i.test(html), "it has tasks; saying otherwise is the defect");
});
