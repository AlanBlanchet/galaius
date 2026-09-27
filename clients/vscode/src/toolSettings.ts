/** Portable values and their allowlist come from the typed Python boundary. */
import { interactCli } from "./interactCli.ts";
import { serverWorkspaceConfigured } from "./workspaceState.ts";

export interface ToolSettingsView {
  configured: boolean; revision: number | null; account_id?: string;
  stale: boolean; values: Record<string, string>; portable_keys: string[];
}
let current: ToolSettingsView | null = null;
let requestGeneration = 0;
export function toolSettingsView(): ToolSettingsView | null { return current; }

export function parseToolSettings(value: unknown): ToolSettingsView {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid settings response");
  const view = value as ToolSettingsView;
  if (typeof view.configured !== "boolean" || typeof view.stale !== "boolean"
      || !Array.isArray(view.portable_keys) || !view.portable_keys.every(k => typeof k === "string" && /^INTERACT_[A-Z_]+$/.test(k))
      || !view.values || typeof view.values !== "object" || Array.isArray(view.values)
      || Object.entries(view.values).some(([k, v]) => !view.portable_keys.includes(k) || typeof v !== "string")
      || (view.configured && (!Number.isSafeInteger(view.revision) || view.revision! < 0
          || typeof view.account_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(view.account_id)))) {
    throw new Error("Invalid settings response");
  }
  return view;
}

async function command(args: string[]): Promise<ToolSettingsView> {
  const result = await interactCli(["config", ...args, "--json-out"]);
  let payload;
  try { payload = JSON.parse(result.stdout); } catch { throw new Error("Personal settings unavailable. Check the matching CLI and server connection; draft retained."); }
  if (result.error || payload?.ok !== true) throw new Error(typeof payload?.message === "string" ? payload.message : "Personal settings refused; draft retained.");
  const view = parseToolSettings(payload);
  if (!view.configured) throw new Error("Server connection changed. Reload before saving; draft retained.");
  return view;
}

export async function refreshToolSettings(): Promise<ToolSettingsView | null> {
  const generation = ++requestGeneration;
  if (!serverWorkspaceConfigured()) { current = null; return null; }
  try {
    const view = await command(["status"]);
    if (generation !== requestGeneration) return current;
    if (!serverWorkspaceConfigured()) { current = null; return null; }
    if (!current || current.account_id !== view.account_id || current.revision! <= view.revision!) current = view;
    return current;
  } catch (error) {
    if (generation !== requestGeneration) return current;
    current = null;
    throw error;
  }
}

export async function saveToolSetting(env: string, value: string | undefined, base: ToolSettingsView | null): Promise<boolean> {
  if (!serverWorkspaceConfigured()) {
    if (base?.configured) throw new Error("Server connection removed. Reload before saving; draft retained.");
    return false;
  }
  if (!base?.configured) throw new Error("Reload personal settings before saving; draft retained.");
  if (!base.portable_keys.includes(env)) return false;
  if (base.stale) throw new Error("Settings cache is stale. Reload online before saving; draft retained.");
  const generation = ++requestGeneration;
  const saved = await command([value === undefined ? "unset" : "set", env, ...(value === undefined ? [] : [value]),
    "--expected-revision", String(base.revision), "--account-id", base.account_id!]);
  if (!serverWorkspaceConfigured()) throw new Error("Server connection removed. Reload before saving; draft retained.");
  if (generation === requestGeneration
      && (!current || current.account_id !== saved.account_id || current.revision! <= saved.revision!)) current = saved;
  return true;
}

export function stripPortableEnvironment(env: Record<string, string>, view: ToolSettingsView | null): void {
  if (view?.configured) for (const key of view.portable_keys) delete env[key];
}
