/** "find a way that we could easily chose what models are ran for what."
 *
 *  Which model an agent runs on is declared in the company file — which is GENERATED from the
 *  prompt repo, so anything the UI wrote there would be erased by the next sync. A choice made in
 *  the editor lives in the agents POLICY (`~/.interact/agents.json`), the one file the spawn
 *  reads: in its `agents` map, beside the profiles, toolsets and provider switches written by hand
 *  or by the CLI. One fact, one file — the choice the panel writes IS the choice the spawn uses.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CLAUDE_TIERS, adoptLegacyChoices, chooseModel, chosenModels, clearChoice,
  looksLikeCriterion, modelChosenFor, policyProblem, profilesIn, ruleReads,
} from "./agentModels.ts";
import { competenceOf, aliasCompetence, aliasScoreLine, aliasTitleLine, coveredIds,
  bareModelName, competenceIndex, shortCompetence, lastModelByAgent, scoreIndex, leaderboardKey, resolvedIndex,
  amongYours,
} from "./competence.ts";
import { boardMatch, boardScoreOf, boardScores } from "./boardScores.ts";

const scratch = () => join(mkdtempSync(join(tmpdir(), "am-")), "agents.json");
const policy = (body: object) => { const p = scratch(); writeFileSync(p, JSON.stringify(body)); return p; };
const fileOf = (p: string) => JSON.parse(readFileSync(p, "utf8")) as Record<string, unknown>;

test("nothing chosen yet is not an error", () => {
  const p = join(mkdtempSync(join(tmpdir(), "am-")), "missing.json");
  assert.deepEqual(chosenModels(p), {});
  assert.equal(modelChosenFor("researcher", p), null);
});

test("a choice survives being written and read back", () => {
  const p = scratch();
  chooseModel("researcher", "ollama/deepseek-v4-pro:cloud", p);
  assert.equal(modelChosenFor("researcher", p), "ollama/deepseek-v4-pro:cloud");
  assert.deepEqual(chosenModels(p), { researcher: "ollama/deepseek-v4-pro:cloud" });
});

test("choosing again replaces rather than accumulates", () => {
  const p = scratch();
  chooseModel("researcher", "a/one", p);
  chooseModel("researcher", "b/two", p);
  assert.deepEqual(chosenModels(p), { researcher: "b/two" });
});

test("clearing hands the agent back to the company file", () => {
  const p = scratch();
  chooseModel("researcher", "a/one", p);
  clearChoice("researcher", p);
  assert.equal(modelChosenFor("researcher", p), null, "a cleared choice must not linger");
  assert.deepEqual(chosenModels(p), {});
});

test("a choice lands in the policy's agents map and leaves the rest of the policy alone", () => {
  /* Profiles, toolsets and provider switches are written by hand or by the CLI; the panel edits
     ONE map in that file and must not flatten, drop or reshape what it did not write. */
  const p = policy({
    profiles: { eyes: "cap.vlm and price.in < 10" }, toolsets: { vision: ["screenshot"] },
    providers: { codex: false },
  });
  chooseModel("researcher", "a/one", p);
  assert.deepEqual(fileOf(p), {
    profiles: { eyes: "cap.vlm and price.in < 10" }, toolsets: { vision: ["screenshot"] },
    providers: { codex: false }, agents: { researcher: "a/one" },
  });
  clearChoice("researcher", p);
  assert.deepEqual(fileOf(p).providers, { codex: false }, "clearing a choice must not clear a switch");
});

test("a rule written by hand shows as the choice — a profile or a criterion, not only an id", () => {
  const p = policy({ profiles: { eyes: "cap.vlm" }, agents: { "visual-critic": "@eyes", librarian: "aa.intelligence >= 30" } });
  assert.equal(modelChosenFor("visual-critic", p), "@eyes");
  assert.equal(modelChosenFor("librarian", p), "aa.intelligence >= 30");
});

test("a profile can be chosen, but only one the policy defines", () => {
  /* `@ghost` would be caught by Python at the next spawn of the one agent wearing it — at 3am.
     The picker only offers profiles that exist, and refuses one that does not. */
  const p = policy({ profiles: { eyes: "cap.vlm" } });
  assert.deepEqual(profilesIn(p), { eyes: "cap.vlm" });
  assert.equal(chooseModel("visual-critic", "@eyes", p), true);
  assert.equal(chooseModel("visual-critic", "@ghost", p), false);
  assert.equal(modelChosenFor("visual-critic", p), "@eyes");
});

test("a corrupt policy does not take the panel down — and is never overwritten by a click", () => {
  /* This file is hand-edited and lives outside the repo; it WILL be malformed one day. It holds
     profiles and toolsets somebody typed, so "recover by rewriting" would destroy their work for
     the sake of one picker click: read as empty, refuse the write, leave it theirs to fix. */
  const p = scratch();
  writeFileSync(p, "{ not json at all");
  assert.deepEqual(chosenModels(p), {});
  assert.equal(chooseModel("researcher", "a/one", p), false);
  assert.equal(readFileSync(p, "utf8"), "{ not json at all");
});

test("only a plausible rule is stored", () => {
  /* The value ends up as a vendor `--model` and, provider-prefixed, selects an endpoint. Storing a
     newline or a flag-shaped string here would be a real hole.

     The length bound moved from 96 to 200 on purpose: this field holds a CRITERION now, not only
     a model id, and `aa.intelligence >= 90% and price.in < 5 and cap.vlm` is a legitimate value.
     The shape guards it exists for — no flags, no newlines, bounded — are unchanged. */
  const p = scratch();
  for (const bad of ["", "  ", "--dangerous", "a/one\nb", "x".repeat(201)]) {
    assert.equal(chooseModel("researcher", bad, p), false, `accepted ${JSON.stringify(bad)}`);
  }
  assert.deepEqual(chosenModels(p), {});
  assert.equal(chooseModel("researcher", "ollama/deepseek-v4-pro:cloud", p), true);
});

test("an agent name is not a path", () => {
  const p = scratch();
  assert.equal(chooseModel("../../etc/passwd", "a/one", p), false);
  assert.equal(chooseModel("", "a/one", p), false);
});

test("the file it writes is readable by a person", () => {
  /* He edits his own config by hand; a one-line blob would be hostile. */
  const p = scratch();
  chooseModel("researcher", "ollama/deepseek-v4-pro:cloud", p);
  const text = readFileSync(p, "utf8");
  assert.ok(text.includes("\n"), "written as an unreadable single line");
  assert.ok(existsSync(p));
});

test("choices made before the policy existed are carried over once, then the old file is gone", () => {
  /* v0.38 kept choices in `agent-models.json`; a choice made there must not vanish from the
     panel the day the store moved. Carried over WITHOUT overriding what the policy already says
     — the policy is the newer word. */
  const dir = mkdtempSync(join(tmpdir(), "am-"));
  const legacy = join(dir, "agent-models.json");
  const p = join(dir, "agents.json");
  writeFileSync(legacy, JSON.stringify({ "web-researcher": "ollama/deepseek-v4-pro:cloud", scraper: "old/one" }));
  writeFileSync(p, JSON.stringify({ agents: { scraper: "new/one" }, providers: { codex: false } }));
  assert.equal(adoptLegacyChoices(legacy, p), 1, "one carried, one already decided by the policy");
  assert.deepEqual(fileOf(p), {
    agents: { scraper: "new/one", "web-researcher": "ollama/deepseek-v4-pro:cloud" }, providers: { codex: false },
  });
  assert.equal(existsSync(legacy), false, "the old file must not linger to be read again");
  assert.equal(adoptLegacyChoices(legacy, p), 0, "running again is a no-op");
});

test("a policy that will not parse is reported at OPEN, naming the file — not only when writing", () => {
  /* The picker silently dropped every profile from an unreadable file and only complained once a
     choice failed to store; the person sat with an empty list wondering where their profiles went. */
  const missing = join(mkdtempSync(join(tmpdir(), "am-")), "absent.json");
  assert.equal(policyProblem(missing), null, "no file is simply no policy");
  const p = policy({ profiles: { eyes: "cap.vlm" } });
  assert.equal(policyProblem(p), null);
  writeFileSync(p, "{ not json at all");
  const problem = policyProblem(p);
  assert.ok(problem && problem.includes(p), `must name the file: ${problem}`);
});

test("Claude Code's own tiers are offered, so a claude agent can be pinned to a tier without an API key", () => {
  /* The picker listed 16 catalog models and no anthropic one — this box reaches Claude through
     the CLI's login, not an API key — so nothing it offered could be carried into a Claude Code
     agent file. The tier aliases Claude Code's frontmatter accepts are always offerable. */
  assert.deepEqual(CLAUDE_TIERS, ["haiku", "sonnet", "opus", "fable"]);
  const p = scratch();
  for (const tier of CLAUDE_TIERS) assert.equal(chooseModel("reviewer", tier, p), true, tier);
});

test("a model row spells out how competent that model is, and where it ranks", () => {
  /* "Models should also spell out their intelligence score, such that we can compare the most
     competents and trust one agent more than another in some situations (isn't absolute)." */
  const models = [
    { id: "big", intelligence_score: 60.2 },
    { id: "mid", intelligence_score: 37.1 },
    { id: "unmeasured" },
  ] as never[];
  assert.equal(competenceOf(models[0], models), "aa.intelligence 60.2 · 1st of 2 scored");
  assert.equal(competenceOf(models[1], models), "aa.intelligence 37.1 · 2nd of 2 scored");
  assert.equal(competenceOf(models[2], models), "not scored");
});

test("a tier alias carries the score of the model it stands for", () => {
  // Round 16's blocking finding: the rows NEAREST THE TOP — Claude Code's `sonnet` / `opus` tiers
  // and whatever the agent runs on today — showed no score, so the one comparison anyone makes,
  // keep the incumbent or switch, could not be made at all.
  const rows = [
    { id: "gpt-5.5", competence: "aa.intelligence 60.2 · 1st of 4" },
    { id: "claude-sonnet-4-6", competence: "aa.intelligence 44.4 · 3rd of 4", asked: "sonnet" },
    { id: "claude-opus-4-7", competence: "aa.intelligence 57.3 · 2nd of 4", asked: "opus" },
  ];
  assert.equal(aliasCompetence("sonnet", rows)?.id, "claude-sonnet-4-6");
  assert.equal(aliasCompetence("opus", rows)?.competence, "aa.intelligence 57.3 · 2nd of 4");
  assert.equal(aliasCompetence("fable", rows), null, "an alias nothing scores stays absent, never invented");
  assert.equal(aliasCompetence("gpt-5.5", rows), null, "a ranked row is not an answer about an alias");
});

test("equal scores share one rank, and say so", () => {
  // Round 17, blocking: enumerating the list printed 1st and 2nd for two models both at 60.2,
  // adjacent on screen — manufacturing exactly the difference the measure denies.
  const all = [{ intelligence_score: 60.2 }, { intelligence_score: 60.2 }, { intelligence_score: 44.6 }];
  assert.equal(competenceOf(all[0], all), "aa.intelligence 60.2 · joint 1st of 3 scored");
  assert.equal(competenceOf(all[1], all), competenceOf(all[0], all));
  assert.equal(competenceOf(all[2], all), "aa.intelligence 44.6 · 3rd of 3 scored", "the tie consumed two places");
});

test("what a ranked list already absorbed is not offered again beneath it", () => {
  // Round 17, blocking: `openai/gpt-5.5` sat as "not scored" directly under `gpt-5.5` at
  // "60.2 · 1st" — 30 browse rows were models the scored section had already ranked.
  const rows = [{ id: "gpt-5.5", competence: "x", covers: ["azure/gpt-5.5", "openai/gpt-5.5"] }];
  // Keyed by BARE name, so the browse catalogue's own spelling of the same model matches too.
  const covered = coveredIds(rows);
  assert.ok(covered.has(bareModelName("gpt-5.5")) && covered.has(bareModelName("openai/gpt-5.5")),
    "the row and every id it absorbed");
  assert.equal(covered.has(bareModelName("claude-opus-4-7")), false);
});

test("an alias nothing scores says so, and a non-model is not called one", () => {
  // Round 17, blocking: `fable` got an honest "nothing scored carries this name" while the
  // INCUMBENT got silence three rows below — the one comparison anyone makes, unanswered.
  const rows = [{ id: "claude-opus-4-7", competence: "aa.intelligence 57.3 · 3rd of 162", asked: "opus" }];
  assert.equal(aliasScoreLine("opus", rows), "aa.intelligence 57.3 · 3rd of 162");
  assert.equal(aliasScoreLine("claude-sonnet-5", rows), "nothing scored carries this name");
  assert.equal(aliasScoreLine("inherit", rows), "not a model — the vendor picks it");
});

test("a name newer than the ranking says so, and the title carries the relative", () => {
  // Round 19, blocking: the incumbent scored for 0 of 45 agents because the registry is older than
  // the fleet. A close relative answers the comparison; it must never be passed off as the model.
  // A row DESCRIPTION stays short — round 19 caught it clipping mid-word, "…so n" — while the
  // TITLE, which wraps instead of truncating, carries the whole sentence.
  const rows = [{ id: "claude-sonnet-4-6", competence: "aa.intelligence 44.4 · joint 36th of 162",
                  asked: "claude-sonnet-5", approximate: true }];
  assert.equal(aliasScoreLine("claude-sonnet-5", rows), "newer than the ranking");
  assert.equal(aliasTitleLine("claude-sonnet-5", rows),
    "newer than the ranking — closest ranked claude-sonnet-4-6 · aa.intelligence 44.4 · joint 36th of 162");
  assert.equal(aliasTitleLine("nope", rows), "nothing scored carries this name");
});

test("a model is the same model across two id namespaces", () => {
  // Round 18, blocking: the dedup dropped 0 of 430. `covers` held LiteLLM ids
  // (`openrouter/anthropic/claude-opus-4.7`) while the browse catalogue speaks OpenRouter
  // (`anthropic/claude-opus-4.7`), so an exact set lookup missed by exactly one prefix and
  // `openai/gpt-5.5` "not scored" kept sitting two rows under `gpt-5.5` at "60.2 · joint 1st".
  // The Python twin is `_bare_model_name` in `src/interact/cli/app.py`; the pairs match its test.
  const same = (a: string, b: string) => assert.equal(bareModelName(a), bareModelName(b), `${a} = ${b}`);
  same("openrouter/anthropic/claude-opus-4.7", "anthropic/claude-opus-4.7");
  same("openai/gpt-5.5", "gpt-5.5");
  same("us.anthropic.claude-opus-4-7", "claude-opus-4.7");
  same("moonshot/kimi-k2.6", "ollama/kimi-k2.6:cloud");
  same("xai/grok-4.3", "xai/grok-4.3-latest");
  same("claude-opus-4-6", "anthropic.claude-opus-4-6-v1");
  same("gemini-3-pro-preview", "replicate/google/gemini-3-pro");
  assert.notEqual(bareModelName("qwen-3-4096-instruct"), bareModelName("qwen-3-instruct"));
  assert.notEqual(bareModelName("gemini-3-pro-image-preview"), bareModelName("gemini-3-pro-preview"));
});

test("what a ranked list absorbed is dropped across namespaces", () => {
  const rows = [{ id: "gpt-5.5", competence: "x", covers: ["openrouter/openai/gpt-5.5"] }];
  const covered = coveredIds(rows);
  assert.ok(covered.has(bareModelName("openai/gpt-5.5")), "the browse catalogue's own spelling");
  assert.ok(covered.has(bareModelName("gpt-5.5")));
  assert.equal(covered.has(bareModelName("claude-opus-4-7")), false);
});

test("nothing is dropped from browse unless a pickable row replaces it", () => {
  // Round 19 regression, mine: the covered set included the TIER rows, whose resolved model sits
  // below the ranked cut — so `anthropic/claude-haiku-4.5` was deleted from the browse list while
  // no `claude-haiku-4-5` row existed to pick instead. Haiku 4.5 became unpickable, under a tier
  // row promising "the claude-haiku-4-5 row pins today's answer".
  const ranked = [{ id: "gpt-5.5", competence: "x", covers: ["openai/gpt-5.5"] }];
  const tierOnly = { id: "claude-haiku-4-5", competence: "y", asked: "haiku" };
  const covered = coveredIds([...ranked, tierOnly]);  // unfiltered: the rule belongs to the function
  assert.equal(covered.has(bareModelName("anthropic/claude-haiku-4.5")), false,
    "a model only a tier row mentions stays browsable");
  assert.ok(covered.has(bareModelName("openai/gpt-5.5")), "what IS ranked is still dropped");
});

test("one lookup answers what any agent's model is measured at", () => {
  // The ask's second half — "trust one agent more than another" — has no surface while the score
  // lives only inside a modal, one agent at a time. The roster needs to answer it per row, for a
  // model spelled however that run happens to spell it.
  const rows = [
    { id: "gpt-5.5", competence: "aa.intelligence 60.2 · joint 1st of 162", covers: ["openai/gpt-5.5"] },
    { id: "claude-sonnet-4-6", competence: "aa.intelligence 44.4 · joint 36th of 162", asked: "sonnet" },
  ];
  const index = competenceIndex(rows);
  assert.equal(index.get(bareModelName("azure/gpt-5.5")), "aa.intelligence 60.2 · joint 1st of 162");
  assert.equal(index.get(bareModelName("openai/gpt-5.5")), "aa.intelligence 60.2 · joint 1st of 162");
  assert.equal(index.get("sonnet"), "aa.intelligence 44.4 · joint 36th of 162", "a tier answers too");
  assert.equal(index.get(bareModelName("nothing-like-this")), undefined);
});

test("a lane states the measure in the width a lane has", () => {
  // Round 20: the full sentence in a lane's chip row crushed its neighbours — the model chip got
  // 12px for 42px of text and rendered as "g" — and the score itself cut to "…1st of 1…". A row
  // beside other rows needs the comparable part only; the full sentence stays where there is room.
  assert.equal(shortCompetence("aa.intelligence 44.4 · joint 36th of 162"), "44.4 · joint 36th");
  assert.equal(shortCompetence("aa.intelligence 60.2 · 1st of 162"), "60.2 · 1st");
  assert.equal(shortCompetence("newer than the ranking"), "newer than the ranking");
  assert.equal(shortCompetence(undefined), undefined);
});

test("a relative's number is marked as one, everywhere it is shown", () => {
  // Round 22: the panels asserted a score for a model that was never measured — `sonnet` and
  // `claude-sonnet-5` both resolve to `claude-sonnet-4-6`, and only the picker kept the
  // "closest ranked" clause. A number nobody can attribute is worse than no number.
  const rows = [
    { id: "claude-sonnet-4-6", competence: "aa.intelligence 44.4 · joint 36th of 162",
      asked: "sonnet", approximate: true },
    { id: "gpt-5.5", competence: "aa.intelligence 60.2 · joint 1st of 162", asked: "gpt-5.5" },
  ];
  const index = competenceIndex(rows);
  assert.equal(index.get("sonnet"),
    "closest ranked claude-sonnet-4-6 · aa.intelligence 44.4 · joint 36th of 162");
  assert.equal(index.get("gpt-5.5"), "aa.intelligence 60.2 · joint 1st of 162");
  assert.equal(shortCompetence(index.get("sonnet")), "≈ 44.4 · joint 36th", "marked, in a column's width");
  assert.equal(shortCompetence(index.get("gpt-5.5")), "60.2 · joint 1st");
});

test("an agent that declares nothing is known by what it last ran on", () => {
  // Round 24, the root cause of "trust one agent more than another": 31 of 45 agents declare
  // `inherit`, which resolves to nothing, so the majority state was a hole and every surface
  // printed one value. The runs on disk record what actually ran.
  const runs = [
    { agent: "tester", model: "sonnet", started_at: 100 },
    { agent: "tester", model: "gpt-5.5", started_at: 300 },   // the most recent wins
    { agent: "tester", model: undefined, started_at: 400 },   // a run that recorded none is skipped
    { agent: "artist", model: "claude-opus-4-7", started_at: 50 },
    { agent: undefined, model: "gpt-5.5", started_at: 999 },
  ];
  const last = lastModelByAgent(runs);
  assert.equal(last.get("tester"), "gpt-5.5", "the newest run that named a model");
  assert.equal(last.get("artist"), "claude-opus-4-7");
  assert.equal(last.get("nobody"), undefined);
});

test("colleagues are ordered by the score, not by a digit in the model's name", () => {
  // Round 27, blocking: the sort read the first number out of the competence SENTENCE, so a row
  // reading "closest ranked anthropic/claude-opus-5 · aa.intelligence 50.7 · 3rd" sorted as 5 and
  // landed seventeen places below a colleague at 30.9. A list built to rank colleagues disagreed
  // with the numbers printed on its own rows.
  const rows = [
    { id: "anthropic/claude-opus-5", competence: "aa.intelligence 50.7 · 3rd of 450", score: 50.7,
      asked: "opus", approximate: true },
    { id: "anthropic/claude-sonnet-5", competence: "aa.intelligence 30.9 · 40th of 450", score: 30.9,
      asked: "sonnet" },
  ];
  const scores = scoreIndex(rows);
  assert.equal(scores.get("opus"), 50.7, "the number the row actually carries");
  assert.equal(scores.get("sonnet"), 30.9);
  assert.equal(scores.get(bareModelName("anthropic/claude-opus-5")), 50.7);
  assert.equal(scores.get("nothing"), undefined);
});

test("a board name reduces the same way on both sides of the panel", () => {
  // Round 28, blocking: the Benchmarks tab counted 433 models where the picker counted 450, from
  // the same 633-row file, because this side re-normalised board NAMES with the id normalizer.
  // That manufactured keys like "deepseek" and "grok" which merge genuinely different models.
  // The Python twin is `_leaderboard_key` in `src/interact/model_catalog.py`.
  assert.equal(leaderboardKey("GPT-5.5 (xhigh)"), "gpt-5-5");
  assert.equal(leaderboardKey("Claude Fable 5.1 (Adaptive Reasoning, Max Effort)"), "claude-fable-5-1");
  assert.equal(leaderboardKey("Claude Opus 4.7"), "claude-opus-4-7");
  // It must NOT strip what the id normalizer strips: a version or a vendor word names the model.
  assert.equal(leaderboardKey("DeepSeek V4 Pro"), "deepseek-v4-pro");
  assert.notEqual(leaderboardKey("DeepSeek V4 Pro"), leaderboardKey("DeepSeek V3"));
  // And it meets an id: the board's name and the catalogue's id reduce to one key.
  assert.equal(leaderboardKey("Claude Sonnet 5"), bareModelName("anthropic/claude-sonnet-5"));
});

test("two spellings of one model are one model", () => {
  // Six rounds running, Split-by-Model reported `sonnet · 31 runs` and `claude-sonnet-5 · 10 runs`
  // as separate models carrying the same score. Someone comparing spend per unit of competence got
  // two wrong answers instead of one right one.
  const rows = [
    { id: "anthropic/claude-sonnet-5", competence: "aa.intelligence 38.4 · 24th of 450",
      score: 38.4, asked: "sonnet" },
    { id: "anthropic/claude-opus-5", competence: "aa.intelligence 50.7 · 3rd of 450", score: 50.7 },
  ];
  const resolved = resolvedIndex(rows);
  assert.equal(resolved.get("sonnet"), "anthropic/claude-sonnet-5", "the tier and the id agree");
  assert.equal(resolved.get(bareModelName("anthropic/claude-sonnet-5")), "anthropic/claude-sonnet-5");
  assert.equal(resolved.get("nothing"), undefined);
});

test("ranked policy shows one ordered cross-provider list with availability deferred to launch", () => {
  assert.deepEqual(ruleReads({name: "tester", rule: "price.in > 0", criterion: true,
    resolves: "model-a", why: null, ranked: [{provider: "alpha", model: "model-a", rank: 0}, {provider: "beta", model: "model-b", rank: 1}]}),
  {model: "model-a", line: "ranked price.in > 0 → alpha/model-a → beta/model-b · availability checked at start"});
});

test("a rule reads as what it RESOLVES to, and a pinned id says it is pinned", () => {
  // "Why is the code reviewer set to sonet instead of being resolved to sonnet? ... we shouldn't
  // write a model, but resolve a model from the constraints." A row printing a bare model id
  // cannot say which of the two happened, so the distinction the mechanism exists for is exactly
  // the one the panel hid. A criterion now names itself AND today's answer.
  assert.deepEqual(
    ruleReads({ name: "code-reviewer", rule: "aa.intelligence >= 90%", criterion: true,
                resolves: "ollama/kimi-k3:cloud", why: null }),
    { model: "ollama/kimi-k3:cloud", line: "resolves aa.intelligence >= 90% → ollama/kimi-k3:cloud" },
  );
  // A criterion has no single answer: each vendor CLI resolves it inside what IT can be pointed
  // at, so one rule legitimately means two models — and saying only one of them would preview a
  // model that refuses the moment the other CLI spawns.
  assert.deepEqual(
    ruleReads({ name: "tester", rule: "aa.intelligence >= 75%", criterion: true,
                resolves: "ollama/kimi-k3:cloud", why: null,
                providers: { claude: "claude-sonnet-4-5", codex: "chatgpt/gpt-5.1-codex-max" } }),
    { model: "ollama/kimi-k3:cloud",
      line: "resolves aa.intelligence >= 75% → claude claude-sonnet-4-5 · codex chatgpt/gpt-5.1-codex-max" },
  );
  // Every switched-on CLI agreeing is the ordinary case, and it reads as one answer.
  assert.deepEqual(
    ruleReads({ name: "tester", rule: "cap.vlm", criterion: true, resolves: "a", why: null,
                providers: { claude: "claude-sonnet-4-5", codex: "claude-sonnet-4-5" } }),
    { model: "claude-sonnet-4-5", line: "resolves cap.vlm → claude-sonnet-4-5" },
  );
  // A CLI that can run nothing clearing the bar says so by name — that is the case worth seeing.
  assert.deepEqual(
    ruleReads({ name: "tester", rule: "aa.intelligence >= 90%", criterion: true,
                resolves: "ollama/kimi-k3:cloud", why: null,
                providers: { claude: null, codex: "chatgpt/gpt-5.1-codex-max" } }),
    { model: "ollama/kimi-k3:cloud",
      line: "resolves aa.intelligence >= 90% → claude nothing · codex chatgpt/gpt-5.1-codex-max" },
  );
  // Nothing clearing it is a STATE, not an absence: "inherit" would be a lie about a rule that
  // is set and currently matches nobody.
  assert.deepEqual(
    ruleReads({ name: "x", rule: "aa.intelligence >= 99% and price.in < 1", criterion: true,
                resolves: null, why: "no configured model clears it" }),
    { model: undefined,
      line: "aa.intelligence >= 99% and price.in < 1 → nothing clears it right now" },
  );
  // A pin still works — it is simply no longer disguised as a resolution.
  assert.deepEqual(
    ruleReads({ name: "web-researcher", rule: "ollama/deepseek-v4-pro:cloud", criterion: false,
                resolves: "ollama/deepseek-v4-pro:cloud", why: null }),
    { model: "ollama/deepseek-v4-pro:cloud", line: "pinned ollama/deepseek-v4-pro:cloud" },
  );
});


test("a row ranks a model among the ones YOU run, not among 450 strangers", () => {
  // "compare the most competents and trust one agent more than another in some situations". On
  // the default board thirteen rows printed the identical string `38.4 · 24th` — a rank against
  // a population none of them competes with. The actionable comparison is the local one.
  const lanes = [
    { model: "anthropic/claude-sonnet-5", score: 38.4 },
    { model: "claude-sonnet-5", score: 38.4 },      // same model, another spelling: ONE competitor
    { model: "gemini/gemini-3.1-pro-preview", score: 57.2 },
    { model: "ollama/kimi-k3:cloud", score: 43.8 },
    { model: "gpt-5.5-pro", score: 60.2 },
  ];
  assert.equal(amongYours(38.4, lanes), "4th of the 4 models you run");
  assert.equal(amongYours(60.2, lanes), "1st of the 4 models you run");
  assert.equal(amongYours(43.8, lanes), "3rd of the 4 models you run");
  // Nothing to compare against is not a rank — a lone model is not "1st".
  assert.equal(amongYours(38.4, [{ model: "a", score: 38.4 }]), undefined);
  assert.equal(amongYours(undefined, lanes), undefined);
  // A tie shares one place, exactly as the board-wide sentence already does.
  assert.equal(
    amongYours(50, [{ model: "a", score: 50 }, { model: "b", score: 50 }, { model: "c", score: 10 }]),
    "joint 1st of the 3 models you run",
  );
});

test("the id normalizer is held to the same table its Python twin reads", () => {
  /* `bareModelName` here and `bare_model_name` in model_catalog.py are one rule in two languages.
     They were two hand-kept copies of the same pairs and they drifted: the suffix strippers ran in
     a FIXED ORDER, so a date sitting behind a hosting tag (`...-20250929-v1:0`) was never reached
     — 74 real litellm ids kept their date and were invisible to every score lookup. One file now
     holds both, so a pair can only change for both at once. */
  const table = JSON.parse(readFileSync(
    join(import.meta.dirname, "..", "..", "..", "tests", "data", "bare_model_names.json"), "utf8",
  )) as { pairs: Record<string, string>; distinct: [string, string][] };
  for (const [id, expected] of Object.entries(table.pairs)) {
    assert.equal(bareModelName(id), expected, id);
  }
  for (const [left, right] of table.distinct) {
    assert.notEqual(bareModelName(left), bareModelName(right),
      `${left} and ${right} are different models and must not collapse together`);
  }
});

test("the panel can store a CRITERION, which is the whole point of having one", () => {
  /* "we shouldn't write a model, but resolve a model from the constraints". The picker offered
     criteria and then refused every one of them: `chooseModel` gated on the MODEL-ID shape, which
     forbids spaces and `>`, so the only thing the panel could write was a literal model id — the
     ask exactly inverted — and it reported the refusal as "is your agents.json valid JSON?",
     blaming a file that was fine. His own live rule, `aa.intelligence >= 35`, was rejected. */
  const p = policy({ agents: {}, profiles: { eyes: "cap.vlm" } });
  assert.equal(chooseModel("code-reviewer", "aa.intelligence >= 35", p), true);
  assert.equal(modelChosenFor("code-reviewer", p), "aa.intelligence >= 35");

  assert.equal(chooseModel("tester", "aa.intelligence >= 90% and price.in < 5", p), true);
  assert.equal(chooseModel("scraper", "cap.vlm", p), true);
  // The two shapes that always worked keep working.
  assert.equal(chooseModel("a", "claude-sonnet-5", p), true);
  assert.equal(chooseModel("b", "@eyes", p), true);

  // What the gate is actually FOR: this value becomes an argv `--model`, so nothing that reads as
  // a flag, and nothing unbounded or multi-line, is stored.
  assert.equal(chooseModel("c", "--dangerously-skip-permissions", p), false);
  assert.equal(chooseModel("d", "aa.intelligence > 1\nrm -rf /", p), false);
  assert.equal(chooseModel("e", "x".repeat(201), p), false);
  assert.equal(chooseModel("f", "   ", p), false);
});

test("a pin written as a profile still carries the model it resolves to", () => {
  /* `@eyes` names no model of its own. Taking the rule verbatim left the row unscored, so an
     agent wearing a profile sorted below every ranked colleague on a board whose whole purpose is
     to rank them. */
  assert.deepEqual(
    ruleReads({ name: "visual-critic", rule: "@eyes", criterion: false,
                resolves: "gemini/gemini-3.1-pro-preview", why: null }),
    { model: "gemini/gemini-3.1-pro-preview", line: "pinned @eyes" },
  );
});

test("without the CLI, a criterion is still not mistaken for a pin", () => {
  /* When `agents policy --json-out` cannot be reached the rows fall back to the file — the same
     file the command would have read. Calling every rule a pin made his live `aa.intelligence >=
     35` read as "your choice aa.intelligence >= 35": one vocabulary for a pin and another for a
     criterion, which is the confusion he wrote in to report. */
  assert.equal(looksLikeCriterion("aa.intelligence >= 35"), true);
  assert.equal(looksLikeCriterion("gui.screenspot > 0.85 and price.in < 10"), true);
  assert.equal(looksLikeCriterion("ollama/deepseek-v4-pro:cloud"), false);
  assert.equal(looksLikeCriterion("claude-sonnet-5"), false);
  // Documented miss: a lone capability needs Python's variable registry to recognise.
  assert.equal(looksLikeCriterion("cap.vlm"), false);
});

test("the panel reads the board itself rather than trusting the CLI to have joined it", () => {
  /* Every number in the product arrived pre-joined from `interact agents models --json-out`, so it
     depended on the VERSION of whichever binary was on PATH. On a machine whose `interact` came
     from a different checkout than the panel, the join never happened: 436 models, 0 scored, and
     three sentences asserting "no ranking carries this model" — false against the Benchmarks tab
     in the same window, reading this very file. */
  const f = join(mkdtempSync(join(tmpdir(), "board-")), "benchmark_scores.json");
  writeFileSync(f, JSON.stringify({ scores: [
    { name: "Claude Opus 5 (Adaptive Reasoning, Max Effort)", intelligence: 50.7 },
    { name: "Claude Opus 5 (Adaptive Reasoning, Low Effort)", intelligence: 39.8 },
    { name: "Meta: Muse Spark 1.3", intelligence: 48.2 },
    { name: "", intelligence: 1 }, { name: "Broken", intelligence: "x" },
  ] }));
  const board = boardScores(f);
  // One model at several effort settings is ONE row at its best number, not four competitors.
  assert.equal(board.get("claude-opus-5"), 50.7);
  assert.equal(board.size, 2, "the nameless and the unscored are not models");
  assert.equal(boardScoreOf("anthropic/claude-opus-5", board, bareModelName), 50.7);
  assert.equal(boardScoreOf("claude-opus-5-20260101", board, bareModelName), 50.7);
  // A missing or unreadable board is EMPTY, never an exception — the caller renders "not known
  // here", which is a different sentence from "not scored".
  assert.equal(boardScores(join(tmpdir(), "nope-does-not-exist.json")).size, 0);
});

test("a board match is exact when it can be and MARKED when it cannot", () => {
  /* A fleet records models under names no board ever prints: a run says `sonnet` where the board
     says "Claude Sonnet 5"; the catalog says `claude-haiku-4-5` where the board says "Claude 4.5
     Haiku"; `gpt-5.1-codex-max` carries a qualifier the board stops short of. Refusing all three
     left thirteen roster rows asserting "no ranking carries this model" about models the board
     ranks. Each weaker route must still say that it IS weaker — the surfaces render `≈` from
     this flag, and asserting a measured score for a model nobody measured is worse than silence. */
  const board = new Map<string, number>([
    ["claude-sonnet-5", 38.4], ["claude-opus-5", 50.7], ["claude-4-5-haiku", 17.6],
    ["gpt-5-1-codex", 24.7],
  ]);
  const hit = (id: string) => boardMatch(id, board, bareModelName);

  // Exact, by the id's own key or its bare form — no mark.
  assert.deepEqual(hit("claude-sonnet-5"), { key: "claude-sonnet-5", score: 38.4, approximate: false });
  assert.deepEqual(hit("anthropic/claude-sonnet-5"),
    { key: "claude-sonnet-5", score: 38.4, approximate: false });

  // Same words, different order — one model, and it says the match was inexact.
  assert.deepEqual(hit("claude-haiku-4-5"), { key: "claude-4-5-haiku", score: 17.6, approximate: true });

  // A TIER names a family; the vendor points it at the best of that family.
  assert.deepEqual(hit("opus"), { key: "claude-opus-5", score: 50.7, approximate: true });
  assert.deepEqual(hit("sonnet"), { key: "claude-sonnet-5", score: 38.4, approximate: true });

  // An extra qualifier the board never printed falls back to the longest prefix it did.
  assert.deepEqual(hit("chatgpt/gpt-5.1-codex-max"),
    { key: "gpt-5-1-codex", score: 24.7, approximate: true });

  // And a model with no relative on the board is simply absent — never a guess.
  assert.equal(hit("some-vendor/entirely-unknown-7"), undefined);
});
