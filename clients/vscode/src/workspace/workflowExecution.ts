/** One selected revision and retry key survive dialog navigation and uncertain delivery. */
import { randomUUID } from "node:crypto";
import { workspaceCommand, WorkspaceCommandError } from "./serverWorkspace.ts";

export interface WorkflowSelection { id: string; revision: string; name: string }
export interface WorkflowRunStatus { id: string; status: string; updatedAt: string; revision: string; key: string }

export class WorkflowExecution {
  workflow: WorkflowSelection;
  values = "{}";
  key: string | null = null;
  run: WorkflowRunStatus | null = null;
  settled = false;
  private busy = false;
  private invocation: string | null = null;
  private command: typeof workspaceCommand;

  constructor(workflow: WorkflowSelection, command: typeof workspaceCommand = workspaceCommand) {
    this.workflow = workflow;
    this.command = command;
  }
  static selection(value: unknown): WorkflowSelection {
    const row = value as { name?: unknown; revision?: unknown; key?: { id?: unknown } } | null;
    if (!row || typeof row.name !== "string" || !WorkflowExecution.uuid(row.revision) || !WorkflowExecution.uuid(row.key?.id)) throw new Error("Invalid workflow record.");
    return { name: row.name, revision: row.revision, id: row.key.id };
  }
  static uuid(value: unknown): value is string {
    return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
  }
  reset(workflow: WorkflowSelection): void {
    if (this.busy) throw new Error("Wait for the current execution request or status check.");
    if (this.key && !this.settled) throw new Error("Check the existing execution first. New execution requires a final status or confirmed rejection without an earlier uncertain attempt.");
    this.workflow = workflow;
    this.key = null; this.run = null; this.invocation = null; this.settled = false;
  }
  private accept(value: unknown): WorkflowRunStatus {
    const run = value as { id?: unknown; status?: unknown; updated_at?: unknown; idempotency_key?: unknown;
      workflow?: { key?: { id?: unknown }; revision?: unknown } } | null;
    if (!run || !WorkflowExecution.uuid(run.id) || typeof run.status !== "string"
        || !["queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"].includes(run.status)
        || typeof run.updated_at !== "string" || !Number.isFinite(Date.parse(run.updated_at))
        || run.idempotency_key !== this.key || run.workflow?.key?.id !== this.workflow.id || run.workflow?.revision !== this.workflow.revision) {
      throw new Error("Run response does not match this request and selected revision. Check execution history.");
    }
    this.run = { id: run.id, status: run.status, updatedAt: run.updated_at, revision: this.workflow.revision, key: this.key! };
    this.settled = ["succeeded", "failed", "cancelled", "interrupted"].includes(run.status);
    return this.run;
  }
  async submit(): Promise<WorkflowRunStatus> {
    if (this.busy) throw new Error("An execution request is already pending.");
    const values: unknown = JSON.parse(this.values);
    if (!values || typeof values !== "object" || Array.isArray(values) || Object.keys(values).length > 32) throw new Error("Input values must be a JSON object with at most 32 names.");
    const invocation = JSON.stringify({ values });
    if (this.invocation !== null && invocation !== this.invocation) throw new Error("Inputs differ from the retained request. Check status, then prepare a New execution.");
    const canConfirmRejection = this.key === null || this.settled;
    this.invocation = invocation;
    this.key ??= randomUUID();
    this.busy = true;
    this.settled = false;
    try {
      const value = await this.command(["workflow-run", this.workflow.id, "--revision", this.workflow.revision,
        "--idempotency-key", this.key, "--invocation-json", this.invocation]);
      return this.accept(value.run);
    } catch (error) {
      if (canConfirmRejection && error instanceof WorkspaceCommandError && ["workflow_conflict", "run_rejected"].includes(error.code)) this.settled = true;
      throw error;
    } finally { this.busy = false; }
  }
  async refresh(): Promise<WorkflowRunStatus | null> {
    if (this.busy) throw new Error("An execution request is already pending.");
    if (!this.key) throw new Error("No request submitted in this session. Execution history lists earlier runs.");
    this.busy = true;
    try {
      const value = await this.command(["workflow-status", this.workflow.id, "--idempotency-key", this.key]);
      return value.run === null ? null : this.accept(value.run);
    } finally { this.busy = false; }
  }
}
