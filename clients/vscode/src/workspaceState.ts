/** Disposable display of typed CLI records. Credentials and writes stay in Python. */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface WorkspaceAgent {
  id: string; revision: string; name: string; role_key: string | null;
  department: string | null; reports_to: string | null;
  criteria: string | null; criteria_weights: string; reasoning: string;
  model: WorkspaceModel | null;
}
export interface WorkspaceModel { id: string; connection: { id: string; revision: string; capability: "http" } }
export function parseWorkspaceModel(value: unknown): WorkspaceModel {
  if (!value || typeof value !== "object") throw new Error("Invalid server model.");
  const model = value as Partial<WorkspaceModel>;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (typeof model.id !== "string" || !model.id || !model.connection || typeof model.connection.id !== "string"
      || typeof model.connection.revision !== "string" || !uuid.test(model.connection.id) || !uuid.test(model.connection.revision)
      || model.connection.capability !== "http") throw new Error("Invalid server model connection.");
  return { id: model.id, connection: { id: model.connection.id, revision: model.connection.revision, capability: "http" } };
}
export interface WorkspaceView {
  configured: true; origin: string; workspace_id: string; state: "current"; writable: boolean;
  graph: { revision: string; root_agent: { id: string; revision: string } | null; agents: WorkspaceAgent[] };
}
let current: WorkspaceView | null = null;
let knownConfigured = false;

export function serverWorkspaceConfigured(): boolean {
  return knownConfigured || fs.existsSync(path.join(os.homedir(), ".interact", "agent-catalog-connection.json"));
}
export function workspaceView(): WorkspaceView | null { return current; }
export function acceptWorkspace(value: WorkspaceView | null, configured = true): void {
  knownConfigured = configured;
  current = value;
}

/** Validate consumed fields at the process boundary, before they reach any control. */
export function parseWorkspace(payload: unknown): WorkspaceView {
  const record = (value: unknown): Record<string, unknown> => {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid workspace response.");
    return value as Record<string, unknown>;
  };
  const string = (value: unknown): string => {
    if (typeof value !== "string") throw new Error("Invalid workspace field.");
    return value;
  };
  const nullable = (value: unknown): string | null => value === null ? null : string(value);
  const id = (value: unknown): string => {
    const text = string(value);
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(text)) throw new Error("Invalid workspace identity.");
    return text;
  };
  const row = record(payload), graph = record(row.graph);
  const origin = new URL(string(row.origin));
  if (!["http:", "https:"].includes(origin.protocol) || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== "/") throw new Error("Invalid server origin.");
  if (row.configured !== true || row.state !== "current" || typeof row.writable !== "boolean" || !Array.isArray(graph.agents)
      || graph.agents.length > 1000 || !/^[0-9a-f]{64}$/.test(string(graph.revision))) throw new Error("Invalid workspace graph.");
  const agents = graph.agents.map(value => {
    const agent = record(value);
    const reasoning = string(agent.reasoning);
    if (!["minimal", "low", "medium", "high", "xhigh", "max", "ultra"].includes(reasoning)) throw new Error("Invalid reasoning level.");
    return { id: id(agent.id), revision: id(agent.revision), name: string(agent.name), role_key: nullable(agent.role_key),
      department: nullable(agent.department), reports_to: agent.reports_to === null ? null : id(agent.reports_to),
      criteria: nullable(agent.criteria), criteria_weights: string(agent.criteria_weights), reasoning,
      model: agent.model === null ? null : parseWorkspaceModel(agent.model) };
  });
  const root = graph.root_agent === null ? null : record(graph.root_agent);
  const rootRef = root ? { id: id(root.id), revision: id(root.revision) } : null;
  if (new Set(agents.map(agent => agent.id)).size !== agents.length || (rootRef && !agents.some(agent => agent.id === rootRef.id && agent.revision === rootRef.revision))) throw new Error("Invalid graph root or duplicate identity.");
  return { configured: true, origin: origin.origin, workspace_id: id(row.workspace_id), state: "current", writable: row.writable,
    graph: { revision: string(graph.revision), root_agent: rootRef, agents } };
}
