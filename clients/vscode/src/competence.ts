/** How competent a model is measured to be, as strings — and nothing else.
 *
 *  A LEAF on purpose: the webview renders a lane's score, and agentModels.ts reads the policy off
 *  disk, so importing that module into the browser bundle would drag fs/os/path in with it. Same
 *  reason agentStatus.ts exists. Pure functions of their arguments; no I/O, no vscode.
 */

/** Everything a RESELLER or a release process appends, in no particular order — applied to a
 *  fixpoint below, so stacking them in any sequence still collapses to one name. Python twin is
 *  _SUFFIXES in src/interact/model_catalog.py; both held to
 *  tests/data/bare_model_names.json, since two hand-kept copies of these pairs drifted. */
const SUFFIXES: RegExp[] = [
  /@.*$/,                                 // a revision pin: @default, @20251001
  /[-@:]?20\d{6}$/,                       // a release date: -20250929
  /[-@:]?20\d{2}-\d{2}-\d{2}$/,           // ...spelled with dashes
  /:[a-z0-9][a-z0-9._-]*$/,               // a hosting tag: :cloud :free :0
  /-v\d+$/,                               // a Bedrock-style revision: -v1
  /-(?:0[1-9]|1[0-2])\d{2}$/,              // a trailing MMDD revision: -0309
  /-0\d{2}$/,                             // a Google-style revision: -002
  /-(?:latest|preview|exp)$/,             // a pointer / maturity suffix
];

/** The model's own name, without who is reselling it — the TWIN of _bare_model_name in
 *  src/interact/cli/app.py, and must stay one (agentModels.test.ts asserts the same pairs that
 *  test does). Exists because the two lists speak different id namespaces: registry ranks
 *  LiteLLM ids (openrouter/anthropic/claude-opus-4.7) while the browse catalogue speaks
 *  OpenRouter (anthropic/claude-opus-4.7), so comparing them literally missed every duplicate by
 *  exactly one prefix. Strips what a RESELLER appends; never what NAMES a different model. */
export function bareModelName(modelId: string): string {
  // A fine-tune IS its own model: stripping its :-separated parts left every one as the single
  // name "ft", collapsing unrelated fine-tunes onto one row.
  if (modelId.startsWith("ft:")) return modelId.toLowerCase().replace(/\./g, "-");
  let name = modelId.split("/").pop()!.toLowerCase();
  name = name.replace(/\s*\[[^\]]*\]\s*$/, "");             // a context variant: [1m]
  name = name.replace(/^[a-z]{2,6}\.(?=[a-z])/, "");           // a region: us. eu. apac.
  const vendorless = name.replace(/^[a-z0-9_-]+\.(?=[a-z])/, "");  // a vendor: anthropic. meta.
  // ...unless what's left is a bare version, in which case that prefix WAS the name
  // (deepseek.v3.2 is not v3.2, and mistral.v3.2 would have collided with it).
  if (!/^v?\d/.test(vendorless)) name = vendorless;
  name = name.replace(/-(?:beta|preview|exp|rc)(?=-|$)/, "");  // a maturity label, mid-id too
  name = name.replace(/-(?:0[1-9]|1[0-2])\d{2}(?=-[a-z])/, "");  // an MMDD revision, month-checked
  // Every SUFFIX rule in one fixpoint, so their ORDER can't matter. Used to run once each in a
  // fixed sequence; a date behind a hosting tag (...-20250929-v1:0) was never reached.
  for (;;) {
    let shorter = name;
    for (const pattern of SUFFIXES) shorter = shorter.replace(pattern, "");
    if (shorter === name || !shorter) break;
    name = shorter;
  }
  return name.replace(/\./g, "-");
}

/** The comparable part of a measure, for a row sitting BESIDE others. A lane has room for a
 *  number and a place, not a sentence: the full form crushed neighbouring chips until a model id
 *  rendered as one letter, then clipped itself. Denominator is the same on every row, so it goes;
 *  full sentence stays in the title, which has room. */
export function shortCompetence(competence: string | undefined): string | undefined {
  if (competence === undefined) return undefined;
  // A RELATIVE's number keeps its mark at every width: panels used to assert a measured score
  // for a model nobody measured; only the picker kept the "closest ranked" clause.
  const relative = competence.startsWith("closest ranked ");
  const core = competence.replace(/^closest ranked \S+ · /, "");
  return (relative ? "≈ " : "") + core.replace(/^aa\.intelligence /, "").replace(/ of \d+$/, "");
}

/** Where a score PLACES in a population: the ordinal, and the "joint" that ties earn.
 *
 *  Two populations ask this — the whole board, and the models this company runs — answer was
 *  written out at each site plus once more in Model.competence() on the Python side. Three
 *  copies of one rule: ordinal suffix, tie convention, count(> score) + 1.
 *
 *  Equal scores share ONE place and say so: printing 1st and 2nd for two models at the same
 *  score manufactures exactly the difference the measure denies.
 */
export function placeIn(score: number, population: readonly number[]): string {
  const rank = population.filter((v) => v > score).length + 1;
  const suffix = [11, 12, 13].includes(rank % 100)
    ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[rank % 10] ?? "th");
  const joint = population.filter((v) => v === score).length > 1 ? "joint " : "";
  return `${joint}${rank}${suffix}`;
}

/** Distinct models a board is running, best score each — derived ONCE per lanes array.
 *  Rebuilding inside every lookup made the paint path quadratic: each of N lanes asked three or
 *  four times, each ask walking all N lanes. */
const FLEET = new WeakMap<object, Map<string, number>>();

function fleetScores(
  lanes: readonly { model?: string; resolvedModel?: string; score?: number }[],
): Map<string, number> {
  const cached = FLEET.get(lanes as object);
  if (cached) return cached;
  const best = new Map<string, number>();
  for (const lane of lanes) {
    const id = lane.resolvedModel || lane.model;
    if (!id || lane.score == null) continue;
    const key = bareModelName(id);
    best.set(key, Math.max(best.get(key) ?? Number.NEGATIVE_INFINITY, lane.score));
  }
  FLEET.set(lanes as object, best);
  return best;
}

/** Where a score places among the models THIS company actually runs.
 *
 *  "compare the most competents and trust one agent more than another in some situations (isn't
 *  absolute)". A rank against the whole board answers a question nobody asked: thirteen rows of
 *  one default view printed the identical string, since every lane was the same model measured
 *  against the same 450 strangers. The actionable rank is the local one — and it's only a rank
 *  when there's something to compare against, so one model alone isn't "1st".
 *
 *  Spellings collapse first: one model under two provider prefixes is ONE competitor, not two.
 */
export function amongYours(
  score: number | undefined,
  lanes: readonly { model?: string; resolvedModel?: string; score?: number }[],
): string | undefined {
  if (score == null) return undefined;
  const best = fleetScores(lanes);
  if (best.size < 2) return undefined;
  return `${placeIn(score, [...best.values()])} of the ${best.size} models you run`;
}

/** How competent a model is, spelled out for a picker row: the namespaced score and its rank
 *  among models carrying the same measure. One measure, never a verdict — a lower-scored model
 *  is often the better fit for a narrow job. Python twin: Model.competence(). */
export function competenceOf(
  model: { intelligence_score?: number | null },
  all: readonly { intelligence_score?: number | null }[],
): string {
  const score = model.intelligence_score;
  if (score == null) return "not scored";
  const scored = all.map((m) => m.intelligence_score).filter((v): v is number => v != null);
  return `aa.intelligence ${score.toFixed(1)} · ${placeIn(score, scored)} of ${scored.length} scored`;
}

/** One row of interact agents models --json-out: a distinct model, its score spelled out, how
 *  many provider aliases collapsed into it, and — when asked for by name — which alias it
 *  stands for. */
export interface ScoredRow {
  id: string; competence: string; aliases?: number; asked?: string;
  /** True when this row is the closest ranked RELATIVE of the asked name, never that name itself:
   *  the registry can be older than the fleet an agent actually runs. */
  approximate?: boolean;
  /** The measure itself. Sorting used to read the first number out of the competence SENTENCE,
   *  which for an approximate row is a digit in the MODEL'S NAME — a colleague at 50.7 sorted as
   *  5, below one at 30.9. Order on this, never on prose. */
  score?: number;
  /** The provider-alias ids this row absorbed, so a second list can drop them. */
  covers?: string[];
}

/** What an ALIAS is measured at: the row the CLI resolved for that name, or null when nothing
 *  scores it. Rows a picker puts nearest the top are aliases, not ids — Claude Code's sonnet /
 *  opus tiers, and whatever the agent runs on today — and while unscored, the one comparison
 *  anyone makes (keep the incumbent or switch) couldn't be made. Never invents a number: an
 *  unscored alias stays unscored and says so. */
export function aliasCompetence(alias: string, rows: readonly ScoredRow[]): ScoredRow | null {
  return rows.find((r) => r.asked === alias) ?? null;
}

/** Every id a ranked list already accounts for — each row plus the provider aliases it absorbed —
 *  so a SECOND list beneath it can drop what's ranked above. Offering openai/gpt-5.5 as "not
 *  scored" directly under gpt-5.5 at "60.2 · 1st" is the picker contradicting itself. */
export function coveredIds(rows: readonly ScoredRow[]): Set<string> {
  // Only a row somebody can actually PICK may hide another. A row resolved by NAME (a tier)
  // points at a model that may sit below the ranked cut; covering it there deleted the only
  // pickable spelling from the list beneath — the tier promised a row that didn't exist.
  return new Set(
    rows.filter((r) => !r.asked).flatMap((r) => [r.id, ...(r.covers ?? [])]).map(bareModelName),
  );
}

/** What to SAY about an alias, always a sentence, never silence. fable got an honest "nothing
 *  scored carries this name" while the incumbent got nothing at all, leaving the one comparison
 *  anyone makes — keep what I run on, or switch — unanswered. A name that isn't a model says
 *  that, instead of being dressed as one. */
export function aliasScoreLine(alias: string, rows: readonly ScoredRow[]): string {
  // inherit is 31 of 45 agents, not a gap: the VENDOR picks the model at spawn, so nothing here
  // can score it. Say that, not dress a non-model as one. Kept SHORT because a row's description
  // truncates — round 19 caught this sentence cut mid-word.
  if (!alias || alias === "inherit") return "not a model — the vendor picks it";
  const hit = aliasCompetence(alias, rows);
  if (!hit) return "nothing scored carries this name";
  return hit.approximate ? "newer than the ranking" : hit.competence;
}

/** The same fact at TITLE length. A title wraps rather than truncates, so it carries the whole
 *  sentence a row can't — including WHICH ranked relative answered for a name the registry
 *  doesn't know, never passed off as the model itself. */
export function aliasTitleLine(alias: string, rows: readonly ScoredRow[]): string {
  if (!alias || alias === "inherit") return "not a model — the vendor picks it at spawn, so nothing scores it";
  const hit = aliasCompetence(alias, rows);
  if (!hit) return "nothing scored carries this name";
  return hit.approximate
    ? `newer than the ranking — closest ranked ${hit.id} · ${hit.competence}`
    : hit.competence;
}

/** What any model is measured at, keyed by BARE name so a run's own spelling resolves — plus
 *  tier aliases, what an agent file usually holds. Built once per repaint.
 *
 *  The ask's second half — "trust one agent more than another" — has no surface while the score
 *  lives only inside the picker modal, one agent at a time. A roster row can answer it only if
 *  something maps that row's model, however spelled, to its measure. */
export function competenceIndex(rows: readonly ScoredRow[]): Map<string, string> {
  const index = new Map<string, string>();
  for (const row of rows) {
    for (const id of [row.id, ...(row.covers ?? [])]) index.set(bareModelName(id), row.competence);
    // The asked-for NAME may not be the model that answered. Say so here, once, so every surface
    // reading this index inherits the attribution instead of each remembering to add it.
    if (row.asked) {
      index.set(row.asked, row.approximate
        ? `closest ranked ${row.id} · ${row.competence}`
        : row.competence);
    }
  }
  return index;
}

/** What each agent LAST actually ran on, newest first.
 *
 *  31 of 45 agents declare inherit — the vendor picks the model at spawn — so a surface reading
 *  only the declaration knows nothing about most of the roster, every row printing the same
 *  value. Run records DO know: they name the model that actually served. A run recording no
 *  model teaches nothing and is skipped, rather than erasing an older one that did.
 */
export function lastModelByAgent(
  runs: readonly { agent?: string; model?: string; started_at?: number }[],
): Map<string, string> {
  const seen = new Map<string, { model: string; at: number }>();
  for (const run of runs) {
    if (!run.agent || !run.model) continue;
    const at = run.started_at ?? 0;
    const held = seen.get(run.agent);
    if (!held || at > held.at) seen.set(run.agent, { model: run.model, at });
  }
  return new Map([...seen].map(([agent, { model }]) => [agent, model]));
}

/** What each name is measured AT, as a number — the sort key for any list of colleagues.
 *
 *  A list ranking colleagues has to agree with the numbers printed on its own rows; the only way
 *  to guarantee that is ordering on the same value the row displays.
 */
export function scoreIndex(rows: readonly ScoredRow[]): Map<string, number> {
  const index = new Map<string, number>();
  for (const row of rows) {
    if (typeof row.score !== "number") continue;
    for (const id of [row.id, ...(row.covers ?? [])]) index.set(bareModelName(id), row.score);
    if (row.asked) index.set(row.asked, row.score);
  }
  return index;
}

/** A leaderboard row's display name reduced to the model it names — the TWIN of
 *  _leaderboard_key in src/interact/model_catalog.py, and must stay one.
 *
 *  Board dresses each model in its effort level (GPT-5.5 (xhigh)), a setting rather than a
 *  different model, so the parenthetical goes and the rest becomes the shape an id reduces to.
 *  Deliberately NOT bareModelName: that strips vendor prefixes and version suffixes, which in a
 *  display NAME are the model — running it over board names counted 433 models where the other
 *  side counted 450, producing keys like "deepseek" that merge different models.
 */
export function leaderboardKey(name: string): string {
  return name.replace(/\s*\(.*$/, "").trim().toLowerCase().replace(/[\s.]+/g, "-");
}

/** Which MODEL a name actually denotes — the id the ranking resolved it to.
 *
 *  A board grouped BY model showed one model twice: once under the tier a run recorded, once
 *  under its full id, each with its own run count and spend and the same score. Grouping on
 *  this makes two spellings one group.
 */
export function resolvedIndex(rows: readonly ScoredRow[]): Map<string, string> {
  const index = new Map<string, string>();
  for (const row of rows) {
    for (const id of [row.id, ...(row.covers ?? [])]) index.set(bareModelName(id), row.id);
    if (row.asked) index.set(row.asked, row.id);
  }
  return index;
}
