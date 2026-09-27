/** The company: who exists, which department, where they can actually run.
 *
 *  Connected workspaces use the current typed CLI graph held in memory. Standalone installs
 *  read the prompt repo's generated org.json. Two things a directory listing cannot carry:
 *
 *  - a HIERARCHY — departments, reporting lines, who pairs with whom — so the panel shows an org,
 *    not a bag of names;
 *  - PROVIDER availability: whether each provider is actually wired (env) or only designed for.
 *    An agent can be part of the company on paper and have nowhere to run today.
 *
 *  Unavailable server records never fall back to a local generated organization.
 */
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { serverWorkspaceConfigured, workspaceView } from "./workspaceState.ts";

export interface OrgProvider {
  label: string;
  /** CLI token a definition-less run of this provider gets RECORDED under (claude, codex).
   *  Present only when confirmed — a guessed binary would silently mis-resolve a run onto the
   *  coordinator. */
  binary?: string | null;
  /** True when a consumer sync target exists NOW. False = shared design, nowhere to run yet. */
  env: boolean;
}

export interface OrgDepartment {
  id: string;
  /** The workplace zone this department belongs in, so the building and the roster agree. */
  room?: string | null;
  mission?: string | null;
  reports_to?: string | null;
  pipeline?: string[] | null;
}

export interface OrgAgent {
  name: string;
  title?: string | null;
  department?: string | null;
  seniority?: string | null;
  role?: string | null;
  reports_to?: string | null;
  pairs_with?: string | null;
  providers?: string[];
  model?: string | null;
  /** Path to the agent's own definition, relative to the prompt repo. */
  def?: string | null;
  description?: string | null;
}

export interface Org {
  source?: "server";
  coordinator: { id: string; title?: string | null };
  providers: Record<string, OrgProvider>;
  departments: OrgDepartment[];
  agents: OrgAgent[];
}

/** Where the prompt repo publishes the company. */
export function orgPath(): string {
  return path.join(os.homedir(), ".claude", "org.json");
}

export function readOrg(file: string = orgPath()): Org | null {
  if (file === orgPath() && serverWorkspaceConfigured()) {
    const view = workspaceView();
    if (!view) return null;
    const agents = view.graph.agents;
    const key = (id: string) => { const agent = agents.find(item => item.id === id); return agent?.role_key ?? agent?.id ?? id; };
    return { source: "server", coordinator: { id: view.graph.root_agent ? key(view.graph.root_agent.id) : "", title: "Server Assistant root" },
      providers: {}, departments: [...new Set(agents.flatMap(agent => agent.department ? [agent.department] : []))].map(id => ({ id })),
      agents: agents.map(agent => ({ name: agent.role_key ?? agent.id, title: agent.name, department: agent.department,
        reports_to: agent.reports_to ? key(agent.reports_to) : null, model: agent.criteria ?? agent.model?.id ?? null })) };
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8")) as Partial<Org>;
    if (!parsed || !Array.isArray(parsed.agents)) return null;
    return {
      coordinator: parsed.coordinator ?? { id: "main" },
      providers: parsed.providers ?? {},
      departments: parsed.departments ?? [],
      agents: parsed.agents,
    };
  } catch {
    // No prompt repo, or a half-written file: the panel shows agents without an org, not an error.
    return null;
  }
}

export interface ResolvedProvider {
  id: string;
  label: string;
  env: boolean;
}

/** An agent with its providers already resolved, so a view never looks one up again. */
export interface OrgSeat extends Omit<OrgAgent, "providers"> {
  providers: ResolvedProvider[];
}

export interface OrgNode extends OrgDepartment {
  agents: OrgSeat[];
}

/** Departments in declared order, each with its people; anyone unfiled gets their own desk.
 *
 *  Unfiled people are KEPT — silently omitting someone because a yaml row is missing is worse
 *  than an obvious "unassigned" bucket: the omission is invisible, the bucket is a prompt to go
 *  file them.
 */
export function orgTree(org: Org): OrgNode[] {
  const resolve = (a: OrgAgent): OrgSeat => ({
    ...a,
    providers: (a.providers ?? []).map((id) => ({
      id,
      label: org.providers[id]?.label ?? id,
      env: org.providers[id]?.env ?? false,
    })),
  });

  const known = new Set(org.departments.map((d) => d.id));
  const nodes: OrgNode[] = org.departments.map((d) => ({
    ...d,
    agents: org.agents.filter((a) => a.department === d.id).map(resolve),
  }));
  const unfiled = org.agents.filter((a) => !a.department || !known.has(a.department));
  if (unfiled.length) {
    nodes.push({
      id: "unassigned",
      room: null,
      mission: "not filed under any department",
      reports_to: null,
      agents: unfiled.map(resolve),
    });
  }
  return nodes;
}

export interface SpawnChoice {
  label: string;
  description?: string;
  detail?: string;
}

/** One provider reported by the installed interact CLI.
 *
 * This is deliberately separate from the prompt repo's `OrgProvider`: org.json says where a
 * role is designed to run, while this answer says which vendor CLI is installed and enabled now.
 */
export interface AgentProviderStatus {
  id: string;
  label: string;
  active: boolean;
  available: boolean;
}

const PROVIDER_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$/;

/** Parse the machine-readable `interact agents providers --json-out` response.
 *
 * Human provider output is intentionally not accepted here. It is a presentation surface and
 * its wording is allowed to change; using it as a protocol made a provider disappear silently.
 */
export function parseAgentProviders(stdout: string): AgentProviderStatus[] {
  let parsed: unknown;
  try { parsed = JSON.parse(stdout); } catch { return []; }
  const providerValue = parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as { providers?: unknown }).providers
    : undefined;
  const rows: unknown[] = Array.isArray(parsed)
    ? parsed
    : Array.isArray(providerValue)
      ? providerValue
      : providerValue && typeof providerValue === "object"
        ? Object.entries(providerValue as Record<string, unknown>).map(([id, value]) =>
          value && typeof value === "object" && !Array.isArray(value)
            ? { ...(value as Record<string, unknown>), id } : value)
        : [];
  const out: AgentProviderStatus[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object" || Array.isArray(row)) continue;
    const value = row as { id?: unknown; name?: unknown; label?: unknown; active?: unknown; available?: unknown };
    const id = typeof value.id === "string" ? value.id : value.name;
    if (typeof id !== "string" || !PROVIDER_ID.test(id)
        || typeof value.active !== "boolean" || typeof value.available !== "boolean") continue;
    const label = typeof value.label === "string" && value.label.trim() ? value.label.trim() : id;
    out.push({ id, label, active: value.active, available: value.available });
  }
  return out;
}

/** Who you can put to work, presented as the company rather than a directory listing.
 *
 *  The picker used to show ~/.claude/agents/<name>.md beside each name — a file path, not a
 *  colleague. With the org in hand it says what the person DOES, which department, and where
 *  they can actually run.
 *
 *  Definitions stay ground truth for what's runnable: an agent the org doesn't mention is still
 *  offered, marked unlisted — invisible-by-missing-row is worse than unlabelled.
 */
export function spawnChoices(
  definitions: readonly string[], org: Org | null,
  options: { includePlain?: boolean; provider?: string } = {},
): SpawnChoice[] {
  const plain: SpawnChoice = { label: "claude", description: "a plain agent, no definition" };
  const includePlain = options.includePlain !== false;
  if (!org) {
    return [...(includePlain ? [plain] : []), ...definitions.map((d) => ({ label: d }))];
  }
  const seats = new Map(orgTree(org).flatMap((d) => d.agents.map((a) => [a.name, { a, d }] as const)));
  const ordered = orgTree(org)
    .flatMap((d) => d.agents.map((a) => a.name))
    .filter((n) => definitions.includes(n));
  const unlisted = definitions.filter((d) => !seats.has(d));

  return [
    ...(includePlain ? [plain] : []),
    ...ordered.map((name) => {
      const { a, d } = seats.get(name)!;
      const where = options.provider ? [options.provider]
        : a.providers.filter((p) => p.env).map((p) => p.id);
      return {
        label: name,
        description: a.title ?? undefined,
        detail: [d.id, a.seniority, where.length ? where.join("+") : org.source === "server" ? "provider checked at launch" : "not wired anywhere"]
          .filter(Boolean).join(" · "),
      };
    }),
    ...unlisted.map((name) => ({ label: name, detail: "not in the company file" })),
  ];
}

/** The company facts the roster needs to name a definition-less run.
 *
 *  The prompt repo's finding: a bare session isn't file-less — the main thread's system prompt IS
 *  instructions.md, so the coordinator has a definition, and the org marks that case
 *  "definition-less" to match. Gives the panel the two things it needs: who coordinates, and
 *  which recorded names are merely a vendor's binary.
 *
 *  Null with no company file — interact works with no prompt repo, and then the provider is all
 *  anyone knows.
 */
export function companyOf(org: Org | null): { coordinator: { id: string; title: string }; binaries: string[] } | null {
  if (!org) return null;
  const binaries = Object.entries(org.providers)
    .map(([id, p]) => (p.binary ?? "").trim() || id)
    .filter(Boolean);
  return {
    coordinator: { id: org.coordinator.id, title: (org.coordinator.title ?? "").trim() || org.coordinator.id },
    binaries,
  };
}

/** Where an agent's system prompt actually lives on disk.
 *
 *  Company file records def RELATIVE to the prompt repo ("agents/researcher.md"), and
 *  ~/.claude/org.json is a symlink INTO that repo — the relative string only means anything once
 *  resolved against the file's REAL location. Opening it unresolved silently opens nothing, the
 *  dead-control failure this project keeps guarding against.
 *
 *  Null when the company file names no definition: better no chip than one that does nothing.
 */
export function definitionFile(agent: string, org: Org | null, orgPath_: string = orgPath()): string | null {
  const rel = org?.agents.find((a) => a.name === agent)?.def;
  if (!rel) return null;
  if (path.isAbsolute(rel)) return rel;
  try {
    return path.resolve(path.dirname(fs.realpathSync(orgPath_)), rel);
  } catch {
    return path.resolve(path.dirname(orgPath_), rel);  // unreadable link: the literal path still helps
  }
}

/** The model an agent declares it should run on, or null for "whatever the session uses".
 *
 *  Half the roster declares inherit — a real value in the org file meaning "do not override" —
 *  but nonsense passed to --model. Translating it to null here keeps that decision in one place
 *  rather than in every caller.
 */
export function modelFor(agent: string, org: Org | null, chosen?: string | null): string | null {
  // A choice made in the editor beats the company file (GENERATED, otherwise the only answer —
  // see agentModels.ts). Passed in rather than imported: this module is loaded directly by the
  // test runner, which cannot resolve an extensionless sibling.
  if (chosen && chosen !== "inherit" && chosen !== "default") return chosen;
  const model = org?.agents.find((a) => a.name === agent)?.model;
  if (!model || model === "inherit" || model === "default") return null;
  return model;
}

/** The argv for starting an agent, so the flags are checkable without spawning anything.
 *
 *  Built here rather than inline in the command: "which flags did we actually pass" used to be
 *  answerable only by running a real agent.
 */
export function spawnArgs(
  opts: {
    task: string;
    provider?: string;
    /** A named role for Start an Agent; null is the intentional generic-session route. */
    agent: string | null;
    cwd?: string | null;
    org: Org | null;
    /** How much autonomy to grant. Null/absent leaves the CLI's own default alone. */
    permissionMode?: string | null;
    providerModes?: Record<string, string>;
    /** An explicit model/profile/criterion supplied by a generic session caller. Named roles leave
     *  this absent so the common launcher reads and resolves their policy exactly once. */
    model?: string | null;
  },
): string[] {
  const args = ["agents", "spawn", opts.task];
  if (opts.provider) args.push("--provider", opts.provider);
  if (opts.agent) args.push("--agent", opts.agent);
  const model = opts.model === undefined
    ? null
    : opts.agent ? modelFor(opts.agent, opts.org, opts.model) : opts.model;
  if (model) args.push("--model", model);
  if (opts.cwd) args.push("--cwd", opts.cwd);
  if (opts.permissionMode) args.push("--permission-mode", opts.permissionMode);
  for (const [provider, mode] of Object.entries(opts.providerModes ?? {})) {
    args.push("--provider-mode", `${provider}=${mode}`);
  }
  return args;
}
