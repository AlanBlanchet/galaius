/** Which model an agent runs on, when YOU chose it.
 *
 *  "find a way that we could easily chose what models are ran for what."
 *
 *  Connected workspaces read agent choices from server revisions and fence local policy writes.
 *  Standalone installs keep choices in ~/.interact/agents.json, the policy their launcher reads.
 *
 *  Deliberately plain readable JSON: he edits his own config by hand, and a file he can't read is
 *  a file he can't fix — also why a file that won't parse is never overwritten from here.
 */
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { serverWorkspaceConfigured, workspaceView } from "./workspaceState.ts";

/** The agents policy — profiles, per-agent model rules, toolsets, provider switches — beside
 *  config.env and NEVER under the debug dir: Python's policy_path() is anchored the same way,
 *  and tests/test_paths.py holds the two together. */
export function agentsPolicyPath(): string {
  return path.join(os.homedir(), ".interact", "agents.json");
}

/** The tier aliases Claude Code's agent-file model: field accepts (sonnet / opus / haiku / fable,
 *  or a full id). Offered by the picker regardless of API keys: a box reaching Claude only
 *  through the CLI's login has no anthropic model in the catalog, so nothing offered could be
 *  carried into a Claude Code agent file otherwise. Python twin: ClaudeCodeProvider._TIERS. */
export const CLAUDE_TIERS = ["haiku", "sonnet", "opus", "fable"] as const;

/** Where v0.38 kept these choices, before the policy existed. Read once, to carry them over. */
export function legacyChoicesPath(): string {
  return path.join(os.homedir(), ".interact", "agent-models.json");
}

/** A model id we're willing to store.
 *
 *  This value becomes an argv --model, and a provider-prefixed one also selects an ENDPOINT, so
 *  it's validated on the way IN, not trusted on the way out: no flags, no whitespace, no
 *  newlines, nothing unbounded.
 */
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._:\/-]{0,95}$/;

/** An agent (or profile) name we are willing to key by — a name, never a path. */
const AGENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/** A rule somebody wrote by hand — a profile, a criterion, an id. SHOWN, never interpreted here:
 *  Python parses it at spawn. One line, bounded, is all the panel asks of it. */
const RULE = /^[^\r\n]{1,200}$/;

type Raw = Record<string, unknown>;

/** The policy as written. parsable is false for a file that exists but won't parse — theirs to
 *  fix, never ours to erase — true for a missing file, which is simply an empty policy. */
function readPolicy(file: string): { raw: Raw; parsable: boolean } {
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf8")) as unknown;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { raw: {}, parsable: false };
    return { raw: raw as Raw, parsable: true };
  } catch (err) {
    return { raw: {}, parsable: (err as NodeJS.ErrnoException)?.code === "ENOENT" };
  }
}

function stringMap(value: unknown, valueOk: RegExp): Record<string, string> {
  const out: Record<string, string> = {};
  if (!value || typeof value !== "object" || Array.isArray(value)) return out;
  for (const [key, v] of Object.entries(value as Raw)) {
    if (typeof v === "string" && AGENT.test(key) && valueOk.test(v)) out[key] = v;
  }
  return out;
}

/** Every agent's rule in the policy. A missing or malformed file reads as "no choices" rather than
 *  throwing: a panel that will not render is worse than a preference that does not show. */
export function chosenModels(file: string = agentsPolicyPath()): Record<string, string> {
  if (file === agentsPolicyPath() && serverWorkspaceConfigured()) {
    return Object.fromEntries((workspaceView()?.graph.agents ?? []).filter(agent => agent.role_key && agent.criteria)
      .map(agent => [agent.role_key!, agent.criteria!]));
  }
  return stringMap(readPolicy(file).raw.agents, RULE);
}

/** The profiles the policy defines — a rule written once, worn by many — for the picker to offer. */
export function profilesIn(file: string = agentsPolicyPath()): Record<string, string> {
  return stringMap(readPolicy(file).raw.profiles, RULE);
}

/** Why the policy can't be read, naming the file — or null when fine or simply absent. Asked at
 *  picker OPEN: an unreadable file used to silently empty the profiles list and only complain
 *  when a choice failed to store, leaving the person wondering where their profiles went. */
export function policyProblem(file: string = agentsPolicyPath()): string | null {
  if (!fs.existsSync(file)) return null;
  return readPolicy(file).parsable ? null : `${file} is not a valid policy (JSON) — profiles and choices are ignored until it is fixed.`;
}

/** The rule chosen for one agent, or null to mean "whatever the company file says". */
export function modelChosenFor(agent: string, file: string = agentsPolicyPath()): string | null {
  return chosenModels(file)[agent] ?? null;
}

/** One agent's model rule as the policy reports it — interact agents policy --json-out. Shape
 *  test ("is this a criterion?") and resolution are BOTH Python's: re-deriving either here would
 *  let the panel disagree with the spawn — the failure this whole mechanism exists to prevent. */
export type PolicyRule = {
  name: string; rule: string; criterion: boolean; resolves: string | null; why: string | null;
  /** What each switched-on vendor CLI would actually run — a criterion resolves inside what THAT
   *  binary can be pointed at, so one rule legitimately means two models. */
  providers?: Record<string, string | null>;
  ranked?: {provider: string; model: string; rank: number}[];
};

/** Whether a rule LOOKS like a criterion — the DEGRADED answer, used only when the CLI can't be
 *  asked. Python's is_criterion is the real one and the spawn uses it; this covers the two shapes
 *  with no vocabulary (a comparison operator, an "and") and deliberately misses the third — a
 *  lone capability name like cap.vlm — since that needs the variable registry, which lives in
 *  Python. A missed one reads as "pinned cap.vlm": wrong but visible. Guessing from a hardcoded
 *  list here is how the panel starts disagreeing with the spawn.
 */
export function looksLikeCriterion(rule: string): boolean {
  return /[<>=]/.test(rule) || / and /.test(rule);
}

/** How a rule reads on a row, and which model it means.
 *
 *  "Why is the code reviewer set to sonet instead of being resolved to sonnet? ... we shouldn't
 *  write a model, but resolve a model from the constraints." A bare id on a row can't say which
 *  happened — so a criterion names itself and today's answer, and a pin is labelled a pin, never
 *  passed off as a resolution.
 */
export function ruleReads(r: PolicyRule): { model: string | undefined; line: string } {
  // resolves first: a pin written as @profile names no model of its own, so taking the rule
  // verbatim left it unscored, sorted below every ranked colleague.
  if (!r.criterion) return { model: r.resolves ?? r.rule, line: `pinned ${r.rule}` };
  if (r.ranked?.length) {
    const order = r.ranked.slice(0, 3).map(candidate => `${candidate.provider}/${candidate.model}`).join(" → ");
    return {model: r.ranked[0].model, line: `ranked ${r.rule} → ${order} · availability checked at start`};
  }
  // A criterion matching nobody is a STATE, not an absence: saying "inherit" would be a lie
  // about a rule that is set, and would hide exactly the case worth seeing.
  const perCli = Object.entries(r.providers ?? {});
  const distinct = new Set(perCli.map(([, model]) => model));
  if (perCli.length && distinct.size > 1) {
    // They disagree, so no single name is the truth — say whose answer is whose.
    const split = perCli.map(([cli, model]) => `${cli} ${model ?? "nothing"}`).join(" · ");
    return { model: r.resolves ?? undefined, line: `resolves ${r.rule} → ${split}` };
  }
  const agreed = perCli.length ? perCli[0][1] : r.resolves;
  if (!agreed) return { model: undefined, line: `${r.rule} → nothing clears it right now` };
  return { model: agreed, line: `resolves ${r.rule} → ${agreed}` };
}

/** Record a choice: a model id, or @profile naming a profile the policy defines. Returns false —
 *  writes nothing — for anything it won't vouch for, or a policy it can't parse. */
export function chooseModel(agent: string, rule: string, file: string = agentsPolicyPath()): boolean {
  if (!AGENT.test(agent)) return false;
  const text = rule.trim();
  // A CRITERION is the expected value here, not an exception — "we shouldn't write a model, but
  // resolve a model from the constraints." This used to gate on the model-ID shape, which forbids
  // spaces and >, so every criterion the picker offered was refused going in — including his own
  // live "aa.intelligence >= 35" — misreported as "is your agents.json valid JSON?", blaming a
  // file that was fine.
  //
  // What the gate is actually for survives: this value becomes an argv --model, so nothing that
  // reads as a flag, nothing multi-line or unbounded. RULE says exactly that; Python parses the
  // rest at spawn — the one place that knows what a criterion means.
  if (text.startsWith("-")) return false;
  // A @name is a PROFILE and must exist: @ghost would otherwise be caught by Python at the next
  // spawn of the one agent wearing it, at 3am. Widening the gate for criteria must not widen it
  // for a typo'd profile too.
  if (text.startsWith("@")) return text.slice(1) in profilesIn(file)
    && editAgents(file, (agents) => { agents[agent] = text; });
  if (!RULE.test(text)) return false;
  return editAgents(file, (agents) => { agents[agent] = text; });
}

/** Hand an agent back to the company file's default. */
export function clearChoice(agent: string, file: string = agentsPolicyPath()): boolean {
  return editAgents(file, (agents) => { delete agents[agent]; });
}

/** Choices from v0.38's own file, carried into the policy ONCE — never overriding a rule the
 *  policy already holds (the newer word) — old file removed so it's never read again. Returns
 *  how many carried; 0 when nothing to do or policy can't take them yet. */
export function adoptLegacyChoices(
  legacy: string = legacyChoicesPath(), file: string = agentsPolicyPath(),
): number {
  if (file === agentsPolicyPath() && serverWorkspaceConfigured()) return 0;
  if (!fs.existsSync(legacy)) return 0;
  const have = chosenModels(file);
  const carry = Object.entries(stringMap(readPolicy(legacy).raw, MODEL)).filter(([agent]) => !(agent in have));
  const taken = editAgents(file, (agents) => { for (const [agent, model] of carry) agents[agent] = model; });
  if (!taken) return 0;  // an unparsable policy: keep the old file, try again next start
  try { fs.unlinkSync(legacy); } catch { /* already gone, or not ours to remove — either is fine */ }
  return carry.length;
}

/** Edit ONLY the agents map, leaving every other key as written — including entries in that map
 *  we wouldn't have written ourselves (a hand-typed rule is theirs, not ours to drop). */
function editAgents(file: string, edit: (agents: Record<string, string>) => void): boolean {
  if (file === agentsPolicyPath() && serverWorkspaceConfigured()) return false;
  const { raw, parsable } = readPolicy(file);
  if (!parsable) return false;  // a broken file is theirs to fix, never ours to erase
  const current = raw.agents;
  const agents = (current && typeof current === "object" && !Array.isArray(current))
    ? { ...(current as Record<string, string>) } : {};
  edit(agents);
  const next: Raw = { ...raw, agents };
  if (Object.keys(agents).length === 0) delete next.agents;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify(next, null, 2) + "\n");
    return true;
  } catch {
    return false;  // a preference that cannot be saved must not break the action that set it
  }
}
