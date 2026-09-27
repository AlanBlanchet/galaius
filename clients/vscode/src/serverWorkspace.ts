import { interactCli } from "./interactCli.ts";
import { acceptWorkspace, parseWorkspace, serverWorkspaceConfigured, type WorkspaceView } from "./workspaceState.ts";

export class WorkspaceCommandError extends Error {
  readonly code: string;
  constructor(message: string, code: string) { super(message); this.code = code; }
}

export async function workspaceCommand(args: readonly string[]): Promise<Record<string, unknown>> {
  const result = await interactCli(["workspace", ...args, "--json-out"]);
  let value: Record<string, unknown>;
  try { value = JSON.parse(result.stdout); } catch { throw new Error("Workspace CLI unavailable. Install the matching Interact build and retry."); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid workspace CLI response.");
  if (value.ok !== true || result.error) {
    const messages: Record<string, string> = {
      authorization: "Server access refused. Sign in with workspace edit permission. Draft retained.",
      conflict: "Server graph changed. Draft retained; reload current graph before retrying.",
      unavailable: "Server unavailable or edit invalid. Draft retained. Check connection, criteria and parent; no offline writes.",
      workflow_conflict: "Selected workflow revision changed. This request was not dispatched. Reload workflows and review the new revision.",
      run_uncertain: "Execution response unavailable; work may still be running. Check status or retry the same request key.",
      run_rejected: "Workflow inputs rejected before dispatch. Correct input names and values, choose New execution, then submit explicitly. An earlier uncertain attempt still requires status or same-key retry.",
    };
    throw new WorkspaceCommandError(messages[String(value.code)] ?? "Workspace command failed; draft retained.", String(value.code));
  }
  return value;
}

export async function refreshWorkspace(): Promise<WorkspaceView | null> {
  try {
    const value = await workspaceCommand(["status"]);
    if (value.configured === false) { acceptWorkspace(null, false); return null; }
    const view = parseWorkspace(value);
    acceptWorkspace(view);
    return view;
  } catch (error) {
    acceptWorkspace(null, serverWorkspaceConfigured());
    throw error;
  }
}
