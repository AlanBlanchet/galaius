/** Acceptance coverage for the cold conversation console and native child activity.
 *
 * These cases are separate from conversationFormat.test.ts because that suite protects the
 * historical run reader.  This file covers the previously impossible operation: starting a root
 * conversation from a cold panel through an explicitly chosen live route.
 */
import { strict as assert } from "node:assert";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import { createRequire } from "node:module";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { restoreEnvironmentAfter } from "../test/fixtures/environment.ts";
import { billingPresentation } from "./billingPresentation.ts";
import { teamSpend } from "./teamSpend.ts";

const require_ = createRequire(import.meta.url);

import {
  CONVERSATION_ACTIVITY_CHILD_LIMIT,
  CONVERSATION_ACTIVITY_EVENT_LIMIT,
  boundedConversationActivityChildren,
  chatDocument,
  conversationApprovalKey,
  conversationApprovalsAfterEvent,
  conversationActivityFragment,
  mergeConversationRunSnapshot,
  conversationStateAfterEvent,
  renderTurn,
  validatedInteractionSubmission,
} from "./conversationFormat.ts";

const commands: never[] = [];

test("the recursive activity tree uses a bounded diagnostic tail", () => {
  // The selected transcript has its own 300-event reader. Repeating that history for every child
  // makes the recursive details tree grow with 300 times the team size.
  assert.ok(CONVERSATION_ACTIVITY_EVENT_LIMIT > 0 && CONVERSATION_ACTIVITY_EVENT_LIMIT <= 40);
  const childVolume = Array.from({ length: 73 }, (_, index) => ({ id: `child-${index}` }));
  const bounded = boundedConversationActivityChildren(childVolume);
  assert.equal(bounded.items.length, CONVERSATION_ACTIVITY_CHILD_LIMIT);
  assert.equal(bounded.items[0].id, `child-${childVolume.length - CONVERSATION_ACTIVITY_CHILD_LIMIT}`,
    "the tree retains the newest useful children, not the oldest launches");
  assert.equal(bounded.omitted, childVolume.length - CONVERSATION_ACTIVITY_CHILD_LIMIT);
  const summary = conversationActivityFragment([{
    run: { run_id: "root", provider: "provider-a", name: "root", status: "running" },
    current_tool: null,
    transcript: [],
    children: [],
    omitted_children: bounded.omitted,
  }], { phase: "ready" });
  assert.match(summary, /49 earlier child runs omitted[\s\S]*Team or session navigation/i,
    "bounded children remain explicitly discoverable through the existing full navigation");
});

function document(consoleState: Record<string, unknown>, extra: Record<string, unknown> = {}): string {
  return chatDocument({
    nonce: "test-nonce",
    commands,
    turns: [],
    name: undefined,
    status: undefined,
    console: consoleState,
    ...extra,
  } as never);
}

test("the cold composer starts on an available session and scales model choice by search", () => {
  // Eighty entries reproduce the catalog volume at which a plain model dropdown becomes a wall.
  // No existing test exercises a cold, route-bound model catalog.
  const models = Array.from({ length: 80 }, (_, index) => ({
    id: `vendor/example-model-${index}`,
    label: `Example model ${index}`,
  }));
  const html = document({
    phase: "ready",
    catalog: {
      version: 1,
      routes: [
        {
          id: "api-route",
          label: "API",
          provider: "provider-a",
          connection: "api",
          availability: "available",
          reason: "",
          charge_path: "metered_api",
          cost_certainty: "known",
          billing_note: "Billed by the API provider.",
          authenticated: true,
          capabilities: ["streaming"],
          models,
          default_model: models[0].id,
          cataloged_at: 1787875200,
        },
        {
          id: "session-route",
          label: "Local session",
          provider: "provider-a",
          connection: "local_session",
          availability: "available",
          reason: "Experimental provider transport; not supported for production workloads.",
          charge_path: "subscription_quota",
          cost_certainty: "unknown",
          billing_note: "Uses the signed-in provider session; account impact is unknown.",
          authenticated: true,
          capabilities: ["streaming", "resume", "cancel", "approvals", "collaboration"],
          models,
          default_model: models[0].id,
          cataloged_at: 1787875200,
        },
        {
          id: "blocked-route",
          label: "Blocked session",
          provider: "provider-b",
          connection: "local_session",
          availability: "policy_blocked",
          reason: "Provider policy does not permit this route.",
          charge_path: "unknown",
          cost_certainty: "unknown",
          billing_note: "Not runnable under the current policy.",
          authenticated: false,
          capabilities: [],
          models: [],
          default_model: null,
          cataloged_at: 1787875200,
        },
      ],
      criteria: ["best-fit"],
      cataloged_at: 1787875200,
    },
  });

  assert.doesNotMatch(html, /id="message"[^>]*disabled/,
    "cold start is the operation under test, so its composer must be enabled");
  assert.match(html, /<select[^>]*id="route"/,
    "provider and connection mode are one route choice, not conflicting controls");
  assert.match(html, /<input[^>]*id="selection"[^>]*list="selection-options"/,
    "a searchable input, rather than eighty option rows, owns exact-model-or-criterion choice");
  assert.match(html, /<option value="session-route" selected/,
    "the catalog-selected available session remains the default");
  assert.doesNotMatch(html, /<option value="api-route" selected/,
    "an available API must never silently displace the available session");
  assert.match(html, /Provider policy does not permit this route/,
    "unavailable routes stay visible with a discrete reason");
  assert.match(html, /class="route-issues"[\s\S]*provider-b[\s\S]*policy blocked[\s\S]*Provider policy/,
    "blocked routes expose their full provider, connection, status and reason outside the clipped selector");
  assert.match(html, /account impact is unknown/i,
    "session authentication must not be presented as zero marginal cost");
  assert.match(html, /id="route-policy"[\s\S]*Experimental provider transport[\s\S]*not supported for production workloads/i,
    "an available route's provider-policy caveat stays visible and separate from billing");
  assert.ok((html.match(/<option data-model=/g) ?? []).length >= models.length,
    "search covers every model in the realistic catalog rather than a hand-picked subset");

  const explicitApi = document({
    phase: "ready",
    catalog: {
      version: 1,
      routes: [
        {
          id: "blocked-session", label: "Blocked session", provider: "provider-a",
          connection: "local_session", availability: "policy_blocked",
          reason: "Session use is blocked by policy.", charge_path: "unknown",
          cost_certainty: "unknown", billing_note: "Not runnable.", models: [],
          cataloged_at: 1787875200,
        },
        {
          id: "metered-api", label: "Metered API", provider: "provider-a",
          connection: "api", availability: "available", reason: "",
          charge_path: "metered_api", cost_certainty: "known",
          billing_note: "Every token is billed by the provider.", models,
          default_model: models[0].id, cataloged_at: 1787875200,
        },
      ],
      criteria: ["best-fit"],
      cataloged_at: 1787875200,
    },
  });
  assert.match(explicitApi, /<option value="" selected disabled>Choose a route<\/option>/,
    "without an available local session, no paid route is chosen on the user's behalf");
  assert.doesNotMatch(explicitApi, /<option value="metered-api" selected/);
  assert.match(explicitApi, /id="message"[^>]*disabled/,
    "Start remains unavailable until the user explicitly chooses the API route");
  assert.match(explicitApi, /refreshComposerAvailability[\s\S]*routeSelect\.selectedOptions/,
    "route change is what enables the composer after billing has become visible");
  assert.match(explicitApi, /data-charge="metered_api"/,
    "the explicit API option carries its charge path into the live disclosure");
  assert.match(explicitApi, /data-note="Every token is billed by the provider\."/,
    "the explicit API option carries its billing note into the live disclosure");
});

test("the console renders every asynchronous state inline", () => {
  // This table owns the lifecycle variants that no historical-transcript test can reach.
  const cases = [
    ["loading", /Loading available routes/i],
    ["empty", /No runnable routes/i],
    ["starting", /Starting the conversation/i],
    ["pending", /is answering/i],
    ["error", /Bridge stopped unexpectedly/i],
  ] as const;

  for (const [phase, expected] of cases) {
    const html = document({
      phase,
      error: phase === "error" ? "Bridge stopped unexpectedly" : undefined,
      catalog: phase === "empty"
        ? { version: 1, routes: [], criteria: [], cataloged_at: 1787875200 }
        : undefined,
    });
    assert.match(html, expected, `${phase} must be a visible state, never a blank panel`);
  }

  const blocked = document({
    phase: "empty",
    catalog: {
      version: 1,
      routes: [{
        id: "blocked", label: "Claude session", provider: "claude",
        connection: "local_session", availability: "policy_blocked",
        reason: "This account policy blocks session use.", charge_path: "unknown",
        cost_certainty: "unknown", billing_note: "Unavailable.", models: [],
        cataloged_at: 1787875200,
      }],
      criteria: [],
      cataloged_at: 1787875200,
    },
  });
  assert.match(blocked, /No runnable routes/i);
  assert.match(blocked, /claude[\s\S]*policy blocked[\s\S]*This account policy blocks session use/i,
    "an all-blocked catalog is empty but still explains every unavailable route");
});

test("an unavailable bridge offers an explicit reload without requiring an agent", () => {
  const html = document({
    phase: "error",
    error: "The compatible local conversation bridge is unavailable.",
  });
  assert.match(html, /data-cold-start="1"/);
  assert.match(html, /id="message"[^>]*disabled/);
  assert.doesNotMatch(html, /select an agent|pick an agent/i);
  assert.match(html, /data-action="reload-conversation"[^>]*>\s*Reload bridge/i,
    "recovery must be a deliberate action that constructs a fresh client");
  assert.match(html, /reload-conversation[\s\S]*postMessage\(\{\s*type:\s*["']reload-conversation["']/,
    "the rendered action must cross the typed webview boundary");
});

test("pending work renders every interaction field and submits the form atomically", () => {
  // One provider request can mix several field kinds. Sending each click independently loses the
  // remaining answers and can accidentally resolve a security-sensitive request too early.
  const html = document({
    phase: "pending",
    active_run_id: "root-1",
    approvals: [{
      run_id: "child-1",
      event: {
        kind: "interaction",
        event_id: "event-approval-1",
        interaction: {
          id: "approval-1", kind: "command_approval", title: "Run workspace command",
          disclosure: ["synthetic-command --flag"],
          fields: [
            { key: "decision", kind: "choice", label: "Decision", required: true,
              options: ["accept", "decline"] },
            { key: "reason", kind: "text", label: "Reason", required: true, options: [] },
            { key: "remember", kind: "boolean", label: "Remember", required: false, options: [] },
          ],
        },
        text: "The provider is waiting for your decision.",
      },
    }],
  });

  assert.match(html, /id="cancel"[^>]*data-run="root-1"/);
  assert.match(html, /<form[^>]*class="interaction-form"[^>]*data-interaction="approval-1"/);
  assert.match(html, /aria-label="Requests waiting for input"/,
    "generic provider questions must not be announced as approvals");
  assert.match(html, /data-field-key="decision"[\s\S]*type="radio"[\s\S]*value="accept"/);
  assert.match(html, /data-field-key="decision"[\s\S]*type="radio"[\s\S]*value="decline"/);
  assert.match(html, /data-field-key="reason"[\s\S]*<textarea[^>]*required/);
  assert.match(html, /data-field-key="remember"[\s\S]*type="checkbox"/);
  assert.match(html, /synthetic-command --flag/,
    "provider disclosure remains visible beside the fields it governs");
  assert.match(html, /type: "interaction"[\s\S]*submission:[\s\S]*interaction_id:[\s\S]*values/,
    "one complete generated InteractionSubmission must cross the webview boundary");
  assert.doesNotMatch(html, /value:\s*["']accept["']/,
    "no timer, default, or hard-coded branch may approve on the user's behalf");
  assert.match(html, /refreshCancel\(msg\.activeRunId/,
    "same-run lifecycle patches must add or remove the cancel control without replacing the prompt");

  const noDecision = document({
    phase: "pending",
    active_run_id: "root-1",
    approvals: [{
      run_id: "child-1",
      event: { kind: "interaction", interaction: {
        id: "approval-2", kind: "command_approval", title: "Run command",
        fields: [{ key: "decision", kind: "choice", label: "Decision", options: [] }],
      } },
    }],
  });
  assert.match(noDecision, /No choices were advertised/i);
  assert.match(noDecision, /class="interaction-submit"[^>]*disabled/,
    "the panel cannot submit a choice the provider did not advertise");

  const optionalNoDecision = document({
    phase: "pending",
    active_run_id: "root-1",
    approvals: [{
      run_id: "child-1",
      event: { kind: "interaction", interaction: {
        id: "optional-choice", kind: "user_input", title: "Optional preference",
        fields: [{ key: "preference", kind: "choice", label: "Preference",
          required: false, options: [] }],
      } },
    }],
  });
  assert.match(optionalNoDecision,
    /data-interaction="optional-choice"[\s\S]*class="interaction-submit" type="submit">/,
    "a provider schema with no required answer can submit an empty values map");
});

test("the extension host accepts only a complete submission matching the pending interaction", () => {
  const interaction = {
    id: "approval-1",
    kind: "user_input",
    title: "Confirm the operation",
    fields: [
      { key: "decision", kind: "choice", label: "Decision", required: true,
        options: ["accept", "decline"] },
      { key: "reason", kind: "text", label: "Reason", required: true, options: [] },
      { key: "remember", kind: "boolean", label: "Remember", required: false, options: [] },
    ],
    disclosure: [],
  } as const;
  const valid = {
    interaction_id: "approval-1",
    values: { decision: "accept", reason: "needed", remember: false },
  };
  assert.deepEqual(validatedInteractionSubmission(interaction, valid), valid);
  const optionalOnly = {
    ...interaction,
    fields: [{ key: "note", kind: "text", label: "Note", required: false, options: [] }],
  } as const;
  assert.deepEqual(
    validatedInteractionSubmission(optionalOnly, { interaction_id: "approval-1", values: {} }),
    { interaction_id: "approval-1", values: {} },
    "an empty map is complete when the pending provider schema has no required fields",
  );
  assert.deepEqual(
    validatedInteractionSubmission(optionalOnly, {
      interaction_id: "approval-1", values: { note: "   " },
    }),
    { interaction_id: "approval-1", values: {} },
    "blank optional text is absent rather than becoming a provider answer",
  );
  const inheritedIdentity = Object.assign(
    Object.create({ interaction_id: "approval-1" }) as Record<string, unknown>,
    { values: valid.values },
  );
  for (const [name, candidate] of [
    ["missing required", { interaction_id: "approval-1", values: { decision: "accept" } }],
    ["unknown field", { ...valid, values: { ...valid.values, injected: "value" } }],
    ["wrong choice kind", { ...valid, values: { ...valid.values, decision: true } }],
    ["wrong boolean kind", { ...valid, values: { ...valid.values, remember: "false" } }],
    ["unadvertised choice", { ...valid, values: { ...valid.values, decision: "always" } }],
    ["wrong interaction", { ...valid, interaction_id: "approval-2" }],
    ["inherited interaction identity", inheritedIdentity],
  ] as const) {
    assert.equal(validatedInteractionSubmission(interaction, candidate), undefined, name);
  }
  assert.equal(validatedInteractionSubmission({
    ...interaction,
    fields: [...interaction.fields, interaction.fields[0]],
  }, valid), undefined, "duplicate advertised keys make the provider schema ambiguous");
});

test("the Chat host forwards only the pending interaction's validated atomic submission", () => {
  const source = fs.readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), "chatView.ts"),
    "utf8",
  );
  assert.match(source, /validatedInteractionSubmission\(interaction, msg\.submission\)/,
    "the hostile webview payload must be checked against the pending provider fields");
  assert.doesNotMatch(source, /typeof msg\.key|typeof msg\.value|values:\s*\{\s*\[msg\.key\]/,
    "the host must not retain the one-field shortcut beside atomic submissions");
  const providerCall = source.indexOf("await this.conversationClient.interact");
  const pendingRemoval = source.indexOf("this.approvals.delete", providerCall);
  const failureBranch = source.indexOf("} catch", providerCall);
  assert.ok(providerCall >= 0 && providerCall < pendingRemoval && pendingRemoval < failureBranch,
    "the pending interaction may disappear only inside the successful provider-response path");
});

test("only the active root event completes a turn and approval identities include their run", () => {
  // Provider-local approval ids and terminal child events both collide at the root unless the
  // extension retains their causal run identity.
  const pending = { phase: "pending", active_run_id: "root-1" } as const;
  const childResult = conversationStateAfterEvent(
    pending,
    { run_id: "child-1", provider: "provider-a", name: "child", root_run_id: "root-1" },
    { kind: "done" },
  );
  const rootResult = conversationStateAfterEvent(
    pending,
    { run_id: "root-1", provider: "provider-a", name: "root", root_run_id: "root-1" },
    { kind: "done" },
  );

  assert.deepEqual(childResult, pending, "a child ending must leave its root turn pending");
  assert.equal(rootResult.phase, "ready");
  assert.equal(rootResult.active_run_id, undefined);
  assert.notEqual(
    conversationApprovalKey("root-1", "approval-1"),
    conversationApprovalKey("child-1", "approval-1"),
    "provider-local approval ids from different runs must remain distinct",
  );
  const approvals = conversationApprovalsAfterEvent([
    { run_id: "root-1", event: { kind: "approval", approval_id: "root-approval" } },
    { run_id: "child-1", event: { kind: "approval", approval_id: "child-approval" } },
  ], { run_id: "child-1", provider: "provider-a", name: "child" }, { kind: "cancelled" });
  assert.deepEqual(approvals.map((approval) => approval.run_id), ["root-1"],
    "terminal child events remove only that child's stale actionable approvals");

  const selectivelyResolved = conversationApprovalsAfterEvent([
    { run_id: "root-1", event: { kind: "interaction", event_id: "event-a",
      interaction: { id: "approval-a", kind: "user_input", title: "A", fields: [] } } },
    { run_id: "root-1", event: { kind: "interaction", event_id: "event-b",
      interaction: { id: "approval-b", kind: "user_input", title: "B", fields: [] } } },
  ], { run_id: "root-1", provider: "provider-a", name: "root" }, {
    kind: "interaction_resolved", event_id: "approval-a:answered",
  });
  assert.deepEqual(
    selectivelyResolved.map((approval) => approval.event.interaction?.id),
    ["approval-b"],
    "one resolution removes only its matching interaction",
  );

});

test("provider failure details never become transcript content", () => {
  // Error payloads can contain provider stderr, account details and paths; the event kind is
  // useful, its backend-authored body is not safe panel content.
  const html = renderTurn({
    kind: "error",
    text: "synthetic credential marker <synthetic-path> private prompt",
  });
  assert.match(html, /provider request failed/i);
  assert.doesNotMatch(html, /synthetic credential marker|<synthetic-path>|private prompt/);
});

test("the causal activity tree shows child identity and escapes every provider field", () => {
  // Native provider children were previously a lossy spawn line; this case uniquely requires a
  // discrete child record with its own task, timing, tools, usage and transcript.
  const hostile = `<img src=x onerror="acquireVsCodeApi().postMessage({type:'pwn'})">`;
  const html = document({ phase: "ready" }, {
    activity: [{
      run: {
        run_id: "root-1",
        parent_run_id: null,
        name: `root ${hostile}`,
        task: `coordinate ${hostile}`,
        provider: "provider-a",
        connection: "local_session",
        requested_model: "vendor/example-model",
        model: "vendor/example-model",
        status: "running",
        started_at: 100,
        finished_at: null,
        input_tokens: 120,
        output_tokens: 30,
        charge_path: "subscription_quota",
      },
      current_tool: `tool ${hostile}`,
      transcript: [{ kind: "text", text: `root answer ${hostile}` }],
      children: [{
        run: {
          run_id: "child-1",
          parent_run_id: "root-1",
          name: `worker ${hostile}`,
          task: `inspect ${hostile}`,
          provider: "provider-a",
          connection: "local_session",
          requested_criterion: "best-fit",
          model: "vendor/example-model-2",
          status: "done",
          started_at: 101,
          finished_at: 103,
          input_tokens: 40,
          output_tokens: 10,
          charge_path: "subscription_quota",
        },
        current_tool: "read",
        transcript: [{ kind: "text", text: `child answer ${hostile}` }],
        children: [],
      }],
    }],
  });

  assert.match(html, /class="activity-tree"/);
  assert.match(html, /data-run="root-1"/);
  assert.match(html, /data-run="child-1"[^>]*data-depth="1"/);
  for (const fact of ["inspect", "example-model-2", "done", "2s", "read", "40", "10"]) {
    assert.match(html, new RegExp(fact), `child details must expose ${fact}`);
  }
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x onerror=&quot;/,
    "task, identity, tool and transcript remain hostile data at the HTML sink");

  const childDocument = chatDocument({
    nonce: "child-nonce",
    commands,
    turns: [],
    name: "worker",
    status: "waiting",
    run: {
      run_id: "child-1", kind: "provider_child", provider: "provider-a",
      task: "inspect", model: "example-model",
    },
    console: { phase: "ready" },
  });
  assert.match(childDocument, /id="message"[^>]*disabled/);
  assert.match(childDocument, /Continue from the parent conversation/i,
    "provider-native children are inspectable but cannot expose a continuation the host rejects");

  const launched = renderTurn({ kind: "spawn", status: "running", text: "inspect" });
  const finished = renderTurn({ kind: "spawn", status: "completed", text: "inspect" });
  assert.match(launched, />spawned</i);
  assert.match(finished, />child finished</i,
    "one child lifecycle must not read as two launches when start and completion both stream");

  for (const terminal of ["done", "failed", "cancelled", "provider_failed"] as const) {
    for (const stale of ["starting", "running", "waiting"] as const) {
      const merged = mergeConversationRunSnapshot({
        run_id: "child-1", provider: "provider-a", name: "child",
        provider_turn_id: "turn-1", status: terminal, finished_at: 103,
      }, {
        run_id: "child-1", provider: "provider-a", name: "child",
        provider_turn_id: "turn-1", status: stale, finished_at: null,
      });
      assert.equal(merged.status, terminal === "provider_failed" ? "failed" : terminal,
        `${terminal} must not regress to stale ${stale} in the same provider turn`);
      assert.equal(merged.finished_at, 103);
    }
  }
  assert.equal(mergeConversationRunSnapshot({
    run_id: "root-1", provider: "provider-a", name: "root",
    provider_turn_id: "turn-1", status: "done",
  }, {
    run_id: "root-1", provider: "provider-a", name: "root",
    provider_turn_id: "turn-2", status: "running",
  }).status, "running", "a genuinely newer provider turn may make the same conversation live again");
});

test("mixed charge paths stay partitioned and root completion remains visually authoritative", () => {
  // One family snapshot binds billing truth and the installed waiting/done contradiction together.
  const activity = [{
    run: {
      run_id: "root-family", provider: "provider-a", name: "root", status: "done",
      started_at: 100, finished_at: 104, charge_path: "subscription_quota",
      cost_certainty: "unknown", input_tokens: 20, output_tokens: 10,
    },
    current_tool: null,
    transcript: [{ kind: "text", text: "authoritative root answer" }],
    children: [{
      run: {
        run_id: "child-api", parent_run_id: "root-family", provider: "provider-b",
        name: "child", status: "done", started_at: 101, finished_at: 103,
        charge_path: "metered_api", cost_certainty: "known",
      },
      current_tool: null,
      transcript: [{ kind: "text", text: "child answer" }],
      children: [],
    }],
  }];
  const billing = billingPresentation([
    { chargePath: "subscription_quota", costCertainty: "unknown", costUsd: null },
    { chargePath: "metered_api", costCertainty: "known", costUsd: null },
  ]);
  const html = document({ phase: "ready" }, { activity, billing });
  assert.match(html, /root[\s\S]*done[\s\S]*4s/i);
  assert.match(html, /authoritative root answer/);
  assert.doesNotMatch(html, /root-family[\s\S]{0,300}(waiting|unknown duration|No activity yet)/i);
  assert.match(html, /class="billing-summary"[\s\S]*mixed/i,
    "distinct subscription and metered charge paths cannot collapse to one billing label");
  assert.match(html, /subscription quota/i);
  assert.match(html, /metered api/i);
});

const terminalFamilyStored = [
  { run_id: "root-family", provider: "provider-a", name: "root", status: "running",
    cost_usd: null, charge_path: "unknown", cost_certainty: "unknown" },
  { run_id: "child-family", parent_run_id: "root-family", provider: "provider-b", name: "child",
    status: "running", cost_usd: null, charge_path: "unknown", cost_certainty: "unknown" },
] as AgentRun[];
const terminalFamilyCurrent = terminalFamilyStored.map((run) => ({
  ...run, status: "done" as const, finished_at: 104,
}));

function terminalFamilyDocument(): string {
  return document({ phase: "ready" }, {
    name: "root",
    status: "done",
    run: terminalFamilyCurrent[0],
    spend: teamSpend(terminalFamilyStored, "root-family", terminalFamilyCurrent),
    activity: [{
      run: terminalFamilyCurrent[0], current_tool: null, transcript: [], children: [{
        run: terminalFamilyCurrent[1], current_tool: null, transcript: [], children: [],
      }],
    }],
  });
}

test("completed root and child facts remove stale running aggregation", () => {
  const html = terminalFamilyDocument();
  assert.match(html, /root[\s\S]*done[\s\S]*child[\s\S]*done/i);
  assert.doesNotMatch(html, /still running/i,
    "the header must derive lifecycle from the same terminal facts as its visible cards");
});

test("unknown team billing stays nonnumeric in rendered accessible copy", () => {
  const html = terminalFamilyDocument();
  assert.match(html, /cost not reported/i);
  assert.doesNotMatch(html, /~?\$0(?:\.0+)?(?![\d.])/i,
    "unknown cost cannot become a plausible visible or accessibility-tree zero");
});

test("a provider burst schedules one family projection and render per UI tick", () => {
  // The activity reader test owns byte/parse bounds; this test owns the independent render batch.
  const source = fs.readFileSync(path.join(
    path.dirname(fileURLToPath(import.meta.url)), "chatView.ts",
  ), "utf8");
  assert.match(source, /private scheduleConversationProjection\(/,
    "stream callbacks need one named batch owner instead of direct render calls");
  const eventHandler = source.match(/private onConversationEvent[\s\S]*?\n  }\n/)?.[0] ?? "";
  assert.match(eventHandler, /scheduleConversationProjection\(\)/);
  assert.doesNotMatch(eventHandler, /this\.render\(\)/,
    "a 50k-history burst must not synchronously reproject once per provider frame");
});

test("the compiled ChatView scheduler projects and renders one time for a provider burst", async (t) => {
  const builtChatView = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)), "..", "out", "chatView.js",
  );
  if (!fs.existsSync(builtChatView)) return t.skip("run npm run compile first");
  const Module = require_("node:module") as {
    _load: (request: string, parent: unknown, isMain: boolean) => unknown;
  };
  const originalLoad = Module._load;
  const vscodeStub = new Proxy({}, { get: () => new Proxy(() => undefined, { get: () => undefined }) });
  Module._load = (request, parent, isMain) => request === "vscode"
    ? vscodeStub
    : originalLoad(request, parent, isMain);
  try {
    delete require_.cache[require_.resolve(builtChatView)];
    const { ChatViewProvider } = require_(builtChatView);
    const view = Object.create(ChatViewProvider.prototype) as {
      conversationProjection?: ReturnType<typeof setTimeout>;
      postConversationActivity: () => void;
      render: () => void;
      scheduleConversationProjection: () => void;
    };
    let projections = 0;
    let renders = 0;
    view.conversationProjection = undefined;
    view.postConversationActivity = () => { projections += 1; };
    view.render = () => { renders += 1; };
    for (let index = 0; index < 100; index += 1) view.scheduleConversationProjection();
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.deepEqual({ projections, renders }, { projections: 1, renders: 1 });
  } finally {
    Module._load = originalLoad;
  }
});

test("the installed compiled ChatView cold-starts a root conversation without choosing an agent", async () => {
  // The source renderer was already correct while the installed VSIX still exposed the old,
  // agent-gated provider.  Load whichever compiled bundle the delivery check names so this one
  // lifecycle assertion can distinguish those artifacts without touching a real editor profile.
  const bundleRoot = process.env.INTERACT_CONVERSATION_BUNDLE_ROOT
    ? path.resolve(process.env.INTERACT_CONVERSATION_BUNDLE_ROOT)
    : path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const builtChatView = path.join(bundleRoot, "chatView.js");
  assert.ok(fs.existsSync(builtChatView), `compiled ChatView is missing at ${builtChatView}`);

  const cases = [
    {
      name: "session route",
      route: {
        id: "session-route", label: "Provider session", provider: "provider-a",
        connection: "local_session", availability: "available", reason: "",
        charge_path: "subscription_quota", cost_certainty: "unknown",
        billing_note: "Uses the signed-in provider session; account impact is unknown.",
        authenticated: true, capabilities: ["streaming"],
        models: [{ id: "provider-a/example-model", provider: "provider-a", capabilities: ["llm"] }],
        default_model: "provider-a/example-model", cataloged_at: 1788220800,
      },
      selectionKind: "criterion",
      selection: "best-fit",
      initiallyEnabled: true,
    },
    {
      name: "explicit metered API route",
      route: {
        id: "api-route", label: "Provider API", provider: "provider-a",
        connection: "api", availability: "available", reason: "",
        charge_path: "metered_api", cost_certainty: "known",
        billing_note: "Every token is billed by the API provider.",
        authenticated: true, capabilities: ["streaming"],
        models: [{ id: "provider-a/example-model", provider: "provider-a", capabilities: ["llm"] }],
        default_model: "provider-a/example-model", cataloged_at: 1788220800,
      },
      selectionKind: "model",
      selection: "provider-a/example-model",
      initiallyEnabled: false,
    },
  ] as const;

  for (const scenario of cases) {
    const starts: Record<string, unknown>[] = [];
    const catalog = {
      version: 1,
      routes: [scenario.route],
      criteria: ["best-fit"],
      cataloged_at: 1788220800,
    };
    const workspaceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
    let receive: ((message: Record<string, unknown>) => void) | undefined;
    let html = "";
    const disposals: Array<() => void> = [];
    const webview = {
      options: {},
      get html(): string { return html; },
      set html(value: string) { html = value; },
      onDidReceiveMessage(listener: (message: Record<string, unknown>) => void) {
        receive = listener;
        return { dispose() {} };
      },
      postMessage: async () => true,
    };
    const view = {
      visible: true,
      webview,
      onDidChangeVisibility: () => ({ dispose() {} }),
      onDidDispose(listener: () => void) {
        disposals.push(listener);
        return { dispose() {} };
      },
      show() {},
    };
    let inert: any;
    inert = new Proxy(() => undefined, {
      get: () => inert,
      construct: () => ({}),
    });
    const vscodeStub = new Proxy({
      commands: { executeCommand: async () => undefined },
      workspace: {
        workspaceFolders: [{ uri: { scheme: "file", fsPath: workspaceRoot } }],
        getConfiguration: () => ({ get: () => "" }),
        onDidChangeWorkspaceFolders: () => ({ dispose() {} }),
      },
      window: {
        showInformationMessage: () => undefined,
        showErrorMessage: () => undefined,
        showTextDocument: async () => undefined,
      },
      Uri: {
        file: (fsPath: string) => ({ scheme: "file", fsPath }),
        from: (value: unknown) => value,
      },
    }, { get: (target, key) => Reflect.has(target, key) ? Reflect.get(target, key) : inert });
    const conversationClient = {
      catalog: async () => catalog,
      start: async (request: Record<string, unknown>) => {
        starts.push(request);
        return await new Promise<never>(() => undefined);
      },
      send: async () => { throw new Error("not used"); },
      cancel: async () => { throw new Error("not used"); },
      interact: async () => { throw new Error("not used"); },
      state: () => "ready",
      dispose() {},
    };
    const localStubs = new Map<string, unknown>([
      ["./agents", { readAgentRuns: () => [], activityOf: () => [] }],
      ["./scopeStore", { scopeStore: () => undefined }],
      ["./permissionModes", {
        knownModes: async () => [],
        describeMode: (_mode: unknown, _known: unknown) => undefined,
      }],
      ["./paths", { agentsDir: () => workspaceRoot }],
      ["./shared", { resolveCommand: () => ["interact", ["mcp"]] }],
      ["./conversationBackend", { resolveConversationBackend: async () => ({
        available: true, command: "interact", args: ["agents", "console"],
      }), conversationExtensionVersion: () => "0.39.0" }],
      ["./runStatus", { runStatusOf: (status: unknown) => status }],
      ["./conversationClient", {
        createConversationClient: () => conversationClient,
        usesConversationTransport: () => true,
      }],
    ]);
    const Module = require_("node:module") as {
      _load: (request: string, parent: unknown, isMain: boolean) => unknown;
    };
    const originalLoad = Module._load;
    Module._load = (request, parent, isMain) => {
      if (request === "vscode") return vscodeStub;
      if (localStubs.has(request)) return localStubs.get(request);
      return originalLoad(request, parent, isMain);
    };
    let provider: { resolveWebviewView: (target: unknown) => void; dispose?: () => void } | undefined;
    try {
      delete require_.cache[require_.resolve(builtChatView)];
      const { ChatViewProvider } = require_(builtChatView);
      provider = new ChatViewProvider({ appendLine() {} });
      provider.resolveWebviewView(view);
      await new Promise((resolve) => setImmediate(resolve));
      await new Promise((resolve) => setTimeout(resolve, 10));

      assert.match(html, /id="composer" data-cold-start="1"/, scenario.name);
      assert.match(html, /<select[^>]*id="route"/, scenario.name);
      assert.match(html, /<input[^>]*id="selection"/, scenario.name);
      assert.doesNotMatch(html, /Pick an agent/, scenario.name);
      if (scenario.initiallyEnabled) {
        assert.doesNotMatch(html, /id="message"[^>]*disabled/, scenario.name);
      } else {
        assert.match(html, /<option value="" selected disabled>Choose a route<\/option>/,
          `${scenario.name} must require an explicit billed-route choice`);
        assert.match(html, /id="message"[^>]*disabled/,
          `${scenario.name} must stay disabled before that choice`);
      }

      assert.ok(receive, `${scenario.name} did not install the webview message listener`);
      receive({
        type: "start",
        text: "Explain this workspace",
        routeId: scenario.route.id,
        selectionKind: scenario.selectionKind,
        selection: scenario.selection,
      });
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(starts.length, 1, `${scenario.name} must start exactly one root conversation`);
      assert.deepEqual(starts[0], {
        route_id: scenario.route.id,
        prompt: "Explain this workspace",
        selection: { [scenario.selectionKind]: scenario.selection },
        workspace_root: workspaceRoot,
      });
      assert.equal(Object.hasOwn(starts[0], "agent"), false,
        `${scenario.name} must not require or synthesize an agent id`);

    } finally {
      provider?.dispose?.();
      for (const dispose of disposals) dispose();
      Module._load = originalLoad;
    }
  }
});

test("a compatible local bridge that exits before initialize survives a fresh extension reload", async () => {
  const bundleRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const builtChatView = path.join(bundleRoot, "chatView.js");
  const workspaceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  const fixtureRoot = path.join(workspaceRoot, "out", "tests", "conversation-exit-reload");
  const bin = path.join(fixtureRoot, "bin");
  const launches = path.join(fixtureRoot, "launches.txt");
  const ready = path.join(fixtureRoot, "ready");
  const release = path.join(fixtureRoot, "release");
  fs.mkdirSync(bin, { recursive: true });
  const executable = path.join(bin, "interact");
  fs.rmSync(executable, { force: true });
  fs.symlinkSync(path.resolve("test/fixtures/conversationFakeHost.ts"), executable);
  fs.rmSync(launches, { force: true });

  let inert: any;
  inert = new Proxy(() => undefined, { get: () => inert, construct: () => ({}) });
  const vscodeStub = new Proxy({
    commands: { executeCommand: async () => undefined },
    workspace: {
      workspaceFolders: [{ uri: { scheme: "file", fsPath: workspaceRoot } }],
      getConfiguration: () => ({ get: () => "" }),
      onDidChangeWorkspaceFolders: () => ({ dispose() {} }),
    },
    window: { showInformationMessage: () => undefined, showErrorMessage: () => undefined,
      showTextDocument: async () => undefined },
    Uri: { file: (fsPath: string) => ({ scheme: "file", fsPath }), from: (value: unknown) => value },
  }, { get: (target, key) => Reflect.has(target, key) ? Reflect.get(target, key) : inert });
  const localStubs = new Map<string, unknown>([
    ["./agents", { readAgentRuns: () => [], activityOf: () => [] }],
    ["./scopeStore", { scopeStore: () => undefined }],
    ["./permissionModes", { knownModes: async () => [], describeMode: () => undefined }],
    ["./paths", { agentsDir: () => workspaceRoot }],
    ["./runStatus", { runStatusOf: (status: unknown) => status }],
    ["./org", { companyOf: () => undefined, definitionFile: () => undefined, readOrg: () => undefined }],
  ]);
  const Module = require_("node:module") as { _load: (request: string, parent: unknown, isMain: boolean) => unknown };
  const originalLoad = Module._load;
  const originalPath = process.env.PATH ?? "";
  const restoreEnvironment = restoreEnvironmentAfter([
    "PATH", "INTERACT_FAKE_CONVERSATION_MODE", "INTERACT_FAKE_CONVERSATION_LOG",
    "INTERACT_FAKE_LAUNCH_LOG", "INTERACT_FAKE_READY_MARKER", "INTERACT_FAKE_RELEASE_MARKER",
  ]);
  Module._load = (request, parent, isMain) => request === "vscode" ? vscodeStub
    : localStubs.has(request) ? localStubs.get(request) : originalLoad(request, parent, isMain);
  process.env.PATH = `${bin}${path.delimiter}${originalPath ?? ""}`;
  process.env.INTERACT_FAKE_CONVERSATION_MODE = "exit_before_initialize";
  process.env.INTERACT_FAKE_LAUNCH_LOG = launches;
  try {
    let html = "";
    const posted: Record<string, unknown>[] = [];
    let receive: ((message: Record<string, unknown>) => void) | undefined;
    let visibilityChanged: (() => void) | undefined;
    const disposals: Array<() => void> = [];
    const webview = { options: {}, get html() { return html; }, set html(value: string) { html = value; },
      onDidReceiveMessage(listener: (message: Record<string, unknown>) => void) {
        receive = listener;
        return { dispose() {} };
      }, postMessage: async (message: Record<string, unknown>) => { posted.push(message); return true; } };
    const view = { visible: true, webview, onDidChangeVisibility(listener: () => void) {
      visibilityChanged = listener; return { dispose() {} };
    },
      onDidDispose(listener: () => void) { disposals.push(listener); return { dispose() {} }; }, show() {} };
    const waitForRecovery = async (attempt: number): Promise<void> => {
      const deadline = Date.now() + 1_000;
      while (!/data-action="reload-conversation"[^>]*>\s*Reload bridge/i.test(html)
          && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      assert.match(html, /exited before initialization \(exit 1; 140 stderr bytes\)[\s\S]*Reload bridge/i,
        `bridge attempt ${attempt} must replace loading with safe actionable recovery`);
      assert.doesNotMatch(html, /private bridge diagnostic|!{3}/);
    };
    delete require_.cache[require_.resolve(builtChatView)];
    const { ChatViewProvider } = require_(builtChatView);
    const provider = new ChatViewProvider({ appendLine() {} });
    try {
      provider.resolveWebviewView(view);
      assert.ok(receive, "the compiled provider must install its reload message boundary");
      await waitForRecovery(1);
      receive({ type: "reload-conversation" });
      assert.match(html, /Loading available routes/i, "reload must visibly begin a fresh attempt");
      await waitForRecovery(2);
      receive({ type: "ready" });
      assert.equal(fs.readFileSync(launches, "utf8"), "launch\nlaunch\n");

      fs.rmSync(ready, { force: true });
      fs.rmSync(release, { force: true });
      process.env.INTERACT_FAKE_CONVERSATION_MODE = "controlled_post_catalog_exit";
      process.env.INTERACT_FAKE_READY_MARKER = ready;
      process.env.INTERACT_FAKE_RELEASE_MARKER = release;
      posted.length = 0;
      receive({ type: "reload-conversation" });
      const readyDeadline = Date.now() + 1_000;
      while ((!fs.existsSync(ready) || !/Local session/.test(html)) && Date.now() < readyDeadline) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      assert.ok(fs.existsSync(ready), "the real bridge must reach catalog before release");
      assert.equal(provider.conversationCatalog()?.routes[0]?.label, "Local session",
        "the dashboard must read the live catalog owned by this ChatView instance");
      const activeDocument = html;
      fs.writeFileSync(release, "release");
      const failureDeadline = Date.now() + 1_000;
      while (!posted.some((message) => message.type === "console-state" && message.error === true)
          && Date.now() < failureDeadline) await new Promise((resolve) => setTimeout(resolve, 10));
      const failure = posted.find((message) => message.type === "console-state" && message.error === true);
      assert.match(String(failure?.message), /exit 1; 140 stderr bytes/);
      assert.doesNotMatch(String(failure?.message), /private active diagnostic|!{3}/);
      assert.equal(provider.conversationCatalog(), undefined,
        "a failed bridge must not leave a stale catalog available to the dashboard");
      visibilityChanged?.();
      assert.equal(html, activeDocument,
        "post-failure visibility must not replace the already-live cold composer document");
      receive({ type: "reload-conversation" });
      const relaunchDeadline = Date.now() + 1_000;
      while (fs.readFileSync(launches, "utf8").split("\n").filter(Boolean).length < 4
          && Date.now() < relaunchDeadline) await new Promise((resolve) => setTimeout(resolve, 10));
      assert.equal(fs.readFileSync(launches, "utf8"), "launch\nlaunch\nlaunch\nlaunch\n",
        "the in-place Reload action must create exactly one fresh client launch");
    } finally {
      provider.dispose();
      for (const dispose of disposals) dispose();
    }
  } finally {
    Module._load = originalLoad;
    restoreEnvironment();
  }
});

test("stored conversation continuation is gated only by its typed resume capability", async () => {
  const bundleRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const builtChatView = path.join(bundleRoot, "chatView.js");
  const builtConversationClient = path.join(bundleRoot, "conversationClient.js");
  assert.ok(fs.existsSync(builtChatView), `compiled ChatView is missing at ${builtChatView}`);
  assert.ok(fs.existsSync(builtConversationClient),
    `compiled conversation client is missing at ${builtConversationClient}`);

  const compiledClient = require_(builtConversationClient) as {
    usesConversationTransport: (run: Record<string, unknown>) => boolean;
  };
  const workspaceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  let currentRun: Record<string, unknown>;
  let currentClient: Record<string, unknown>;
  let cliCalls = 0;
  const localStubs = new Map<string, unknown>([
    ["./agents", { readAgentRuns: () => [currentRun], activityOf: () => [] }],
    ["./scopeStore", { scopeStore: () => undefined }],
    ["./permissionModes", {
      knownModes: async () => [],
      describeMode: (_mode: unknown, _known: unknown) => undefined,
    }],
    ["./paths", { agentsDir: () => workspaceRoot }],
    ["./shared", { resolveCommand: () => ["interact", ["mcp"]] }],
    ["./conversationBackend", { resolveConversationBackend: async () => ({
      available: true, command: "interact", args: ["agents", "console"],
    }), conversationExtensionVersion: () => "0.39.0" }],
    ["./runStatus", { runStatusOf: (status: unknown) => status }],
    ["./org", { companyOf: () => undefined, definitionFile: () => undefined,
      readOrg: () => undefined }],
    ["./interactCli", { interactCli: async () => {
      cliCalls += 1;
      return { error: "unexpected legacy continuation", stdout: "" };
    } }],
    ["./conversationClient", {
      createConversationClient: () => currentClient,
      usesConversationTransport: compiledClient.usesConversationTransport,
    }],
  ]);
  const Module = require_("node:module") as {
    _load: (request: string, parent: unknown, isMain: boolean) => unknown;
  };
  const originalLoad = Module._load;
  let inert: any;
  inert = new Proxy(() => undefined, {
    get: () => inert,
    construct: () => ({}),
  });
  const vscodeStub = new Proxy({
    commands: { executeCommand: async () => undefined },
    workspace: {
      workspaceFolders: [{ uri: { scheme: "file", fsPath: workspaceRoot } }],
      getConfiguration: () => ({ get: () => "" }),
      onDidChangeWorkspaceFolders: () => ({ dispose() {} }),
    },
    window: {
      showInformationMessage: () => undefined,
      showErrorMessage: () => undefined,
      showTextDocument: async () => undefined,
    },
    Uri: {
      file: (fsPath: string) => ({ scheme: "file", fsPath }),
      from: (value: unknown) => value,
    },
  }, { get: (target, key) => Reflect.has(target, key) ? Reflect.get(target, key) : inert });
  Module._load = (request, parent, isMain) => {
    if (request === "vscode") return vscodeStub;
    if (localStubs.has(request)) return localStubs.get(request);
    return originalLoad(request, parent, isMain);
  };

  const cases = [
    { name: "stored API cancel-only", connection: "api", capabilities: ["cancel"], canResume: false },
    { name: "stored API with resume", connection: "api", capabilities: ["resume", "cancel"], canResume: true },
    { name: "stored session cancel-only", connection: "local_session", capabilities: ["cancel"], canResume: false },
    { name: "stored session with resume", connection: "local_session",
      capabilities: ["resume", "cancel"], canResume: true },
  ] as const;

  try {
    delete require_.cache[require_.resolve(builtChatView)];
    const { ChatViewProvider } = require_(builtChatView);
    for (const scenario of cases) {
      const clientSends: Array<{ runId: string; text: string }> = [];
      const posted: Record<string, unknown>[] = [];
      const disposals: Array<() => void> = [];
      let receive: ((message: Record<string, unknown>) => void) | undefined;
      let html = "";
      cliCalls = 0;
      currentRun = {
        run_id: `stored-${scenario.connection}-${scenario.capabilities.join("-")}`,
        kind: "conversation",
        provider: "provider-a",
        name: scenario.name,
        task: "Continue this stored conversation",
        cwd: workspaceRoot,
        project: "interact",
        model: "provider-a/example-model",
        provider_session_id: "provider-session",
        connection: scenario.connection,
        capabilities: [...scenario.capabilities],
        tools: [],
        charge_path: "unknown",
        cost_certainty: "unknown",
        cost_usd: null,
        foreign: false,
        started_at: 1788220800,
        status: "waiting",
      };
      currentClient = {
        catalog: async () => ({ version: 1, routes: [], criteria: [], cataloged_at: 1788220800 }),
        start: async () => { throw new Error("not used"); },
        send: async (runId: string, text: string) => {
          clientSends.push({ runId, text });
          return currentRun;
        },
        cancel: async () => { throw new Error("not used"); },
        interact: async () => { throw new Error("not used"); },
        state: () => "ready",
        dispose() {},
      };
      const webview = {
        options: {},
        get html(): string { return html; },
        set html(value: string) { html = value; },
        onDidReceiveMessage(listener: (message: Record<string, unknown>) => void) {
          receive = listener;
          return { dispose() {} };
        },
        postMessage: async (message: Record<string, unknown>) => {
          posted.push(message);
          return true;
        },
      };
      const view = {
        visible: true,
        webview,
        onDidChangeVisibility: () => ({ dispose() {} }),
        onDidDispose(listener: () => void) {
          disposals.push(listener);
          return { dispose() {} };
        },
        show() {},
      };
      let provider: {
        resolveWebviewView: (target: unknown) => void;
        show: (runId: string) => void;
        dispose: () => void;
      } | undefined;
      try {
        provider = new ChatViewProvider({ appendLine() {} });
        provider.resolveWebviewView(view);
        await new Promise((resolve) => setImmediate(resolve));
        await new Promise((resolve) => setTimeout(resolve, 10));
        provider.show(String(currentRun.run_id));
        await new Promise((resolve) => setImmediate(resolve));

        const textareaDisabled = /id="message"[^>]*\bdisabled\b/.test(html);
        const sendDisabled = /<button type="submit" disabled>Send<\/button>/.test(html);
        const nonResumableExplanation = /cannot be resumed|does not support resuming/i.test(html);
        assert.ok(receive, `${scenario.name} did not install the hostile-webview boundary`);
        receive({ type: "send", text: "Continue from the stored context" });
        await new Promise((resolve) => setImmediate(resolve));
        await new Promise((resolve) => setImmediate(resolve));
        const acknowledgements = posted
          .filter((message) => message.type === "sent")
          .map((message) => message.ok);

        assert.deepEqual({
          textareaDisabled,
          sendDisabled,
          nonResumableExplanation,
          clientCalls: clientSends.length,
          cliCalls,
          acknowledgements,
        }, {
          textareaDisabled: !scenario.canResume,
          sendDisabled: !scenario.canResume,
          nonResumableExplanation: !scenario.canResume,
          clientCalls: scenario.canResume ? 1 : 0,
          cliCalls: 0,
          acknowledgements: [scenario.canResume],
        }, scenario.name);
      } finally {
        provider?.dispose();
        for (const dispose of disposals) dispose();
      }
    }
  } finally {
    delete require_.cache[require_.resolve(builtChatView)];
    Module._load = originalLoad;
  }
});

test("failed starts restore the exact prompt and a bridge crash never retries itself", () => {
  // The old composer already restores a failed continuation; this separate case binds the same
  // invariant to first-turn creation and prevents a process crash from becoming a paid retry.
  const html = document({ phase: "error", error: "bridge crashed" });

  assert.match(html, /if \(inFlight && !box\.value\) box\.value = inFlight/,
    "the failed first prompt must return without reconstruction or loss");
  assert.match(html, /msg\.type !== "started"/,
    "the same acknowledgement path must cover creation and continuation");
  assert.doesNotMatch(html, /setTimeout\([^)]*(connect|start|retry)/i,
    "a crash is visible and ambiguous, so only a new user action may start another process");
  const source = fs.readFileSync(path.join(
    path.dirname(fileURLToPath(import.meta.url)), "chatView.ts",
  ), "utf8");
  assert.match(source, /private rendered: string \| null \| undefined/,
    "a rendered cold composer must be distinct from an absent document so crash handling cannot replace and lose its draft");
  assert.match(source, /const repaintColdDocument = !this\.consoleState\.catalog[\s\S]*?if \(repaintColdDocument\) \{[\s\S]*?this\.rendered = undefined;[\s\S]*?this\.render\(\)/,
    "only a cold bridge failure may invalidate Loading and repaint the actionable recovery state");
  assert.match(source, /this\.conversationGeneration === generation && this\.conversationClient === client/,
    "callbacks from a replaced bridge must not overwrite the reloaded client state");
  assert.match(source, /this\.rendered = current\?\.run_id \?\? null/,
    "render must record the already-live cold document with the non-absent sentinel");
});

test("the webview script enables an explicit API and patches cancel lifecycle in a real DOM", () => {
  // Source assertions cannot catch TDZ/runtime DOM faults. Chrome executes the exact generated
  // document and one dependent interaction sequence: explicit route selection, pending, ready.
  const chrome = process.env.CHROME_BIN ?? "/usr/bin/google-chrome";
  assert.ok(fs.existsSync(chrome), "the extension visual-test image must provide Chrome");
  const artifactDir = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)), "..", "..", "out", "tests", "conversation-console-dom",
  );
  const profile = path.join(artifactDir, `profile-${process.pid}-${Date.now()}`);
  const page = path.join(artifactDir, `page-${process.pid}-${Date.now()}.html`);
  const childProfile = path.join(artifactDir, `child-profile-${process.pid}-${Date.now()}`);
  const childPage = path.join(artifactDir, `child-page-${process.pid}-${Date.now()}.html`);
  fs.mkdirSync(artifactDir, { recursive: true });
  const html = document({
    phase: "ready",
    catalog: {
      version: 1,
      routes: [
        {
          id: "blocked-session", label: "Blocked session", provider: "provider-a",
          connection: "local_session", availability: "policy_blocked", reason: "Blocked.",
          charge_path: "unknown", cost_certainty: "unknown", billing_note: "Unavailable.",
          models: [], cataloged_at: 1787875200,
        },
        {
          id: "metered-api", label: "Metered API", provider: "provider-a", connection: "api",
          availability: "available", reason: "Experimental transport; not for production.", charge_path: "metered_api",
          cost_certainty: "known", billing_note: "Every token is billed.",
          models: [{ id: "example", provider: "provider-a", capabilities: ["llm"] }],
          default_model: "example", cataloged_at: 1787875200,
        },
      ],
      criteria: [],
      cataloged_at: 1787875200,
    },
    approvals: [{
      run_id: "root-1",
      event: {
        kind: "interaction", interaction: {
          id: "approval-narrow", kind: "command_approval",
          title: "item/commandExecution/requestApproval",
          fields: [
            { key: "decision", kind: "choice", label: "Decision", options: ["A", "B"] },
            { key: "reason", kind: "text", label: "Reason", options: [] },
            { key: "remember", kind: "boolean", label: "Remember", options: [] },
          ],
        },
      },
    }],
  }).replace(
    "const vscode = acquireVsCodeApi();",
    "const vscode = { messages: [], postMessage(message) { this.messages.push(message); }, " +
      "getState() { return {}; }, setState() {} };",
  ).replace("</script>", `
const harnessRoute = document.getElementById("route");
harnessRoute.value = "metered-api";
harnessRoute.dispatchEvent(new Event("change", { bubbles: true }));
const selectionOnly = !vscode.messages.some((message) =>
  message?.type === "start" || message?.type === "send");
const harnessBox = document.getElementById("message");
const harnessSubmit = document.querySelector('#composer button[type="submit"]');
const harnessStatus = document.createElement("span");
harnessStatus.className = "status";
harnessStatus.textContent = "running";
document.body.prepend(harnessStatus);
const selected = !harnessBox.disabled && !harnessSubmit.disabled &&
  harnessBox.placeholder.includes("Ask the new conversation") &&
  document.getElementById("billing").textContent.includes("metered_api") &&
  document.getElementById("route-policy").textContent.includes("not for production");
window.dispatchEvent(new MessageEvent("message", { data: {
  type: "console-state", message: "working", error: false, canSend: false, activeRunId: "root-1"
} }));
const pending = document.getElementById("cancel")?.dataset.run === "root-1" && harnessBox.disabled &&
  harnessBox.placeholder === "working";
window.dispatchEvent(new MessageEvent("message", { data: {
  type: "console-state", runStatus: "running"
} }));
const tracked = document.getElementById("cancel")?.dataset.run === "root-1" &&
  harnessBox.disabled && harnessStatus.textContent === "running";
window.dispatchEvent(new MessageEvent("message", { data: {
  type: "console-state", message: "", error: false, canSend: true, activeRunId: null,
  runStatus: "waiting"
} }));
const ready = !document.getElementById("cancel") && !harnessBox.disabled &&
  harnessStatus.textContent === "waiting" &&
  harnessBox.placeholder.includes("Ask the new conversation");
document.body.dataset.identity = "preserve-me";
harnessBox.value = "half-typed draft";
window.dispatchEvent(new MessageEvent("message", { data: {
  type: "console-state",
  message: "The conversation bridge stopped unexpectedly (exit 1; 140 stderr bytes). Reload the window to try again.",
  error: true,
  canSend: false,
  activeRunId: null
} }));
const activeFailurePreservesDocument = document.body.dataset.identity === "preserve-me" &&
  harnessBox.value === "half-typed draft" && harnessBox.disabled &&
  document.getElementById("console-live-state")?.textContent.includes("exit 1; 140 stderr bytes");
document.documentElement.style.width = "207px";
document.body.style.width = "207px";
const harnessApproval = document.querySelector(".approval");
const approvalBounds = harnessApproval?.getBoundingClientRect();
const approvalFits = Boolean(harnessApproval && approvalBounds) &&
  harnessApproval.scrollWidth <= harnessApproval.clientWidth && approvalBounds.right <= 207 &&
  Array.from(harnessApproval.querySelectorAll("button")).every((button) =>
    button.getBoundingClientRect().right <= approvalBounds.right);
// Native DOM semantics and the real message listener prove that inputs do nothing on their own,
// an incomplete form stays local, and one explicit submit sends every typed field exactly once.
const interactionForm = harnessApproval.querySelector("form.interaction-form");
const retryMarkup = harnessApproval.outerHTML;
const beforeIncomplete = vscode.messages.length;
interactionForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
const incompleteRejected = vscode.messages.length === beforeIncomplete &&
  !interactionForm.querySelector(".interaction-error").hidden;
const choiceB = interactionForm.querySelector('[data-field-key="decision"] input[value="B"]');
const reason = interactionForm.querySelector('[data-field-key="reason"] textarea');
const remember = interactionForm.querySelector('[data-field-key="remember"] input');
choiceB.click();
reason.value = "Because it is required";
remember.click();
const beforeSubmit = vscode.messages.length;
interactionForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
interactionForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
const interactions = vscode.messages.slice(beforeSubmit)
  .filter((message) => message?.type === "interaction");
const atomicSubmission = interactions.length === 1 && interactions[0].runId === "root-1" &&
  interactions[0].submission?.interaction_id === "approval-narrow" &&
  JSON.stringify(interactions[0].submission.values) ===
    JSON.stringify({ decision: "B", reason: "Because it is required", remember: true });
// A host error retains the approval; its activity replay restores enabled controls for one retry.
window.dispatchEvent(new MessageEvent("message", { data: { type: "activity", html: retryMarkup } }));
const retryForm = document.querySelector("form.interaction-form");
retryForm.querySelector('[data-field-key="decision"] input[value="A"]').click();
retryForm.querySelector('[data-field-key="reason"] textarea').value = "Retry";
const beforeRetry = vscode.messages.length;
retryForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
const retryInteractions = vscode.messages.slice(beforeRetry)
  .filter((message) => message?.type === "interaction");
const hostErrorRetainsDecision = retryInteractions.length === 1 &&
  retryInteractions[0].submission?.interaction_id === "approval-narrow" &&
  retryInteractions[0].submission.values.decision === "A";
// Same-run delivery is transcript then activity. Updating the transcript must not delete the
// stable activity target before the first-and-only approval patch arrives.
document.getElementById("transcript-content").insertAdjacentHTML("beforeend",
  '<details class="turn turn-tool" data-tool-id="stable-tool"><summary>tool</summary><p>input</p></details>');
const expandedTool = document.querySelector("details.turn-tool");
if (expandedTool) expandedTool.open = true;
const expandedMarkup = expandedTool?.outerHTML ?? "";
window.dispatchEvent(new MessageEvent("message", { data: {
  type: "transcript", html: expandedMarkup + '<p id="streamed-answer">answer</p>'
} }));
window.dispatchEvent(new MessageEvent("message", { data: {
  type: "activity", html: '<article id="approval-after-transcript">approval choices</article>'
} }));
const stablePatches = Boolean(document.getElementById("streamed-answer")) &&
  Boolean(document.getElementById("approval-after-transcript")) &&
  Boolean(document.querySelector("details.turn-tool[open]"));
window.dispatchEvent(new MessageEvent("message", { data: {
  type: "transcript", html: '<p id="replacement-answer">replacement</p>'
} }));
const absentToolsAreRemoved = !document.querySelector("details.turn-tool") &&
  Boolean(document.getElementById("replacement-answer"));
// A first-and-only provider event can race the fresh document. The loaded document must announce
// that its listeners exist so the extension can replay the current approval/activity snapshot.
const listenerReady = vscode.messages.some((message) => message?.type === "ready");
document.body.dataset.harness = selectionOnly && selected && pending && tracked && ready && approvalFits &&
  incompleteRejected && atomicSubmission && hostErrorRetainsDecision &&
  stablePatches && absentToolsAreRemoved && listenerReady && activeFailurePreservesDocument
  ? "ok" : "failed";
</script>`);
  const childHtml = chatDocument({
    nonce: "child-runtime-nonce",
    commands,
    turns: [],
    name: "synthetic child",
    status: "done",
    readOnly: true,
    run: {
      run_id: "child-runtime", kind: "provider_child", provider: "provider-a",
      task: "inspect", model: "vendor/example-child",
    },
    console: { phase: "ready" },
  }).replace(
    "const vscode = acquireVsCodeApi();",
    "const vscode = { messages: [], postMessage(message) { this.messages.push(message); }, " +
      "getState() { return {}; }, setState() {} };",
  ).replace("</style>", `
body {
  --vscode-button-background: rgb(14, 99, 156);
  --vscode-button-foreground: rgb(255, 255, 255);
  --vscode-button-secondaryBackground: rgb(49, 49, 49);
  --vscode-disabledForeground: rgb(130, 130, 130);
}
</style>`).replace("</script>", `
document.documentElement.style.width = "207px";
document.body.style.width = "207px";
const childBox = document.getElementById("message");
const childSend = document.querySelector('#composer button[type="submit"]');
const enabledSend = childSend.cloneNode(true);
enabledSend.disabled = false;
enabledSend.style.position = "absolute";
enabledSend.style.left = "-10000px";
document.body.append(enabledSend);
const disabledStyle = getComputedStyle(childSend);
const enabledStyle = getComputedStyle(enabledSend);
const semanticLock = childBox.disabled && childSend.disabled;
const visuallyMuted = Number(disabledStyle.opacity) < Number(enabledStyle.opacity) &&
  disabledStyle.backgroundColor !== enabledStyle.backgroundColor &&
  disabledStyle.boxShadow === "none" && disabledStyle.cursor === "not-allowed";
const childBounds = childSend.getBoundingClientRect();
document.body.dataset.childHarness = semanticLock && visuallyMuted && childBounds.right <= 207
  ? "ok" : "failed";
</script>`);
  fs.writeFileSync(page, html);
  fs.writeFileSync(childPage, childHtml);
  try {
    const rendered = execFileSync(chrome, [
      "--headless=new", "--no-sandbox", "--disable-gpu", `--user-data-dir=${profile}`,
      "--dump-dom", page,
    ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    assert.match(rendered, /data-harness="ok"/,
      "the inline script must execute and preserve explicit route/cancel state in the DOM");
    const chatViewSource = fs.readFileSync(path.join(
      path.dirname(fileURLToPath(import.meta.url)), "chatView.ts",
    ), "utf8");
    assert.match(chatViewSource, /msg\?\.type === "ready"[\s\S]*replayConversationView\(\)/,
      "ChatView must replay approval/activity only after the new document reports listener readiness");
    const childRendered = execFileSync(chrome, [
      "--headless=new", "--no-sandbox", "--disable-gpu", `--user-data-dir=${childProfile}`,
      "--dump-dom", childPage,
    ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    assert.match(childRendered, /data-child-harness="ok"/,
      "an inspect-only child must be semantically disabled and visibly muted at 207px");
  } finally {
    if (fs.existsSync(page)) fs.unlinkSync(page);
    if (fs.existsSync(profile)) fs.rmSync(profile, { recursive: true });
    if (fs.existsSync(childPage)) fs.unlinkSync(childPage);
    if (fs.existsSync(childProfile)) fs.rmSync(childProfile, { recursive: true });
  }
});
