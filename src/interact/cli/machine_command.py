"""CLI for enrolling and keeping this machine connected to Interact."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from cyclopts import App, Parameter

from interact_core import WorkflowNode

from interact.machines import MachineFiles, MachineRunner, ScriptExecution, connect_command

machine_app = App(name="machine", help="Connect this computer as a workflow machine.")


@machine_app.command(name="connect")
def machine_connect(
    server_url: Annotated[str | None, Parameter(name="--server")] = None,
    workspace_id: Annotated[UUID | None, Parameter(name="--workspace")] = None,
    machine_id: Annotated[UUID | None, Parameter(name="--machine")] = None,
    permission_ceiling: Literal["read_only", "full_access"] = "read_only",
    working_directory: Annotated[Path | None, Parameter(name="--working-directory")] = None,
    configure_only: bool = False,
) -> None:
    """Save a UI-issued machine credential and connect outbound to its server."""
    asyncio.run(connect_command(server_url, workspace_id, machine_id, None, permission_ceiling, working_directory, not configure_only))


@machine_app.command(name="file-roots")
def machine_file_roots(*roots: str) -> None:
    """Owner-only, on this machine: the folders (relative to its working directory) workflow file
    nodes may read and write. No argument prints them; arguments replace them."""
    runner = MachineRunner()
    config = runner.load()
    if roots:
        config = runner.update(lambda current: current.model_copy(update={"file_roots": tuple(roots)}))
    print(json.dumps({"working_directory": str(config.working_directory), "file_roots": list(config.file_roots)}))


@machine_app.command(name="agents")
def machine_agents(state: Literal["on", "off"] | None = None) -> None:
    """Owner-only, on this machine: whether workflow agent steps may run here (an agent CLI can read
    any file of this user). No argument prints it."""
    runner = MachineRunner()
    config = runner.update(lambda current: current.model_copy(update={"run_agents": state == "on"})) if state is not None else runner.load()
    print(json.dumps({"run_agents": config.run_agents}))


@machine_app.command(name="script-roots")
def machine_script_roots(*roots: str) -> None:
    """Owner-only, on this machine: the folders (relative to its working directory) Script steps
    may run a file from. Never inside or around a file root, so no workflow can write beside a
    script it runs. No argument prints them; arguments replace them; "" alone clears them."""
    runner = MachineRunner()
    config = runner.load()
    if roots:
        config = runner.update(lambda current: current.model_copy(update={"script_roots": tuple(root for root in roots if root)}))
    usable, refused = config.usable_script_roots()
    base = config.working_directory.resolve()
    print(json.dumps({"working_directory": str(config.working_directory), "script_roots": [root.relative_to(base).as_posix() for root in usable], "refused": list(refused)}))


@machine_app.command(name="agent-roots")
def machine_agent_roots(*roots: str) -> None:
    """Owner-only, on this machine: the folders (relative to its working directory) you may start
    agents in from the web, in any folder beneath them. Never inside or around a file or script
    root. No argument prints them; arguments replace them; "" alone clears them."""
    runner = MachineRunner()
    config = runner.load()
    if roots:
        config = runner.update(lambda current: current.model_copy(update={"agent_roots": tuple(root for root in roots if root)}))
    usable, refused = config.usable_agent_roots()
    base = config.working_directory.resolve()
    print(json.dumps({"working_directory": str(config.working_directory), "agent_roots": [root.relative_to(base).as_posix() for root in usable], "refused": list(refused),
                      "agent_permission": config.agent_permission, "run_agents": config.run_agents}))


@machine_app.command(name="agent-permission")
def machine_agent_permission(scope: Literal["read_only", "workspace_write", "full_access"] | None = None) -> None:
    """Owner-only, on this machine: what agents started from the web may do — read_only, edit
    files (workspace_write, the default), or full_access (no permission prompts at all). No
    argument prints it."""
    runner = MachineRunner()
    config = runner.update(lambda current: current.model_copy(update={"agent_permission": scope})) if scope is not None else runner.load()
    print(json.dumps({"agent_permission": config.agent_permission}))


@machine_app.command(name="approve-script")
def machine_approve_script(machine_id: UUID | None = None, source_digest: str | None = None, *, pending: bool = False, yes: bool = False) -> None:
    """Owner-only: allowlist one exact script digest on `machine_id` before any workflow can
    dispatch it there (threat-modeler mitigation #1 — never trust workflow write-scope alone). It
    first shows what the digest stands for — every saved Script step carrying it: its code and
    language, or the machine file and how it starts (run on that machine, the file's content as it
    is now and its git state) — then asks. --pending needs no digest: every saved Script step placed
    on this machine (or `machine_id`) that is not approved yet, one question per version. --yes
    approves without asking."""
    from interact.server_workspace import ServerWorkspace
    try:
        workspace = ServerWorkspace.configured()
        if pending:
            machine = machine_id or MachineRunner().load().machine_id
            waiting = _pending_steps(workspace.workflows(), machine, set(workspace.script_approvals(machine)))
            if not waiting:
                print(json.dumps({"ok": True, "machine_id": str(machine), "approved": [], "skipped": [], "message": "every saved Script step on this machine is approved"}))
                return
            approved, skipped = [], []
            for digest, steps in waiting.items():
                (approved if _approve(workspace, machine, digest, steps, yes) else skipped).append(digest)
            print(json.dumps({"ok": True, "machine_id": str(machine), "approved": approved, "skipped": skipped}))
            return
        if machine_id is None or source_digest is None:
            raise PermissionError("name the machine and the digest, or pass --pending")
        steps = [(workflow.name, node) for workflow in workspace.workflows() for node in workflow.nodes if node.impl.kind == "script" and node.impl.approval_digest(node.config) == source_digest]
        if not steps:
            raise PermissionError("no saved workflow step carries this digest: save the workflow, then approve it")
        if not _approve(workspace, machine_id, source_digest, steps, yes):
            raise PermissionError("not approved")
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, "machine_id": str(machine_id), "source_digest": source_digest}))


def _pending_steps(workflows, machine_id: UUID, approved: set[str]) -> dict[str, list[tuple[str, WorkflowNode]]]:
    """Saved Script steps placed on `machine_id` whose version it has not approved, by digest (one
    approval covers every step running that same version)."""
    waiting: dict[str, list[tuple[str, WorkflowNode]]] = {}
    for workflow in workflows:
        for node in workflow.nodes:
            if node.impl.kind == "script" and node.placement.machine is not None and node.placement.machine.id == machine_id:
                digest = node.impl.approval_digest(node.config)
                if digest not in approved:
                    waiting.setdefault(digest, []).append((workflow.name, node))
    return waiting


def _approve(workspace, machine_id: UUID, digest: str, steps: list[tuple[str, WorkflowNode]], yes: bool) -> bool:
    """Shows what `digest` runs, asks unless `yes`, approves it; whether it was approved."""
    for name, node in steps:
        print(_described_step(name, node, machine_id))
    if not yes:
        if not sys.stdin.isatty():
            raise PermissionError("confirm in a terminal, or pass --yes")
        if input(f"Approve this on machine {machine_id}? [y/N] ").strip().lower() not in {"y", "yes"}:
            return False
    workspace.approve_script(machine_id, digest)
    return True


def _described_step(workflow: str, node: WorkflowNode, machine_id: UUID) -> str:
    """What running `node` does, as the machine owner reads it before approving it."""
    impl = node.impl
    language = {"python": "Python", "shell": "Shell"}[impl.language]
    lines = [f"Workflow “{workflow}”, step “{node.label}”"]
    if impl.origin == "inline":
        source = str(node.config.get("source", ""))
        program = ScriptExecution.select(impl.language, source).description
        lines.append(f"Language: {language}, run by {program}")
        shown = source.splitlines()[:80]
        lines += ["Code:", *(f"  {line}" for line in shown), *([f"  … {len(source.splitlines()) - len(shown)} more lines"] if len(source.splitlines()) > len(shown) else [])]
        return "\n".join(lines)
    spec = impl.script_file(node.config)
    lines += [f"Language: {language}", f"File: {spec.path} (sha256 {spec.file_digest})", f"Arguments: {' '.join(spec.args) or 'none'}",
              f"Starts in: {spec.cwd or 'the file’s folder'}"]
    try:
        config = MachineRunner().load()
    except Exception:
        config = None
    if config is None or config.machine_id != machine_id:
        lines.append("Program: " + (ScriptExecution.select(impl.language, "", spec).description if spec.interpreter or impl.language == "shell" else "not checked here; python3 or uv, depending on the file's declared packages"))
        lines.append("Not checked here: run this on that machine to compare the file as it is now.")
        return "\n".join(lines)
    try:
        files = MachineFiles(config=config, area="scripts")
        content = files.script_bytes(files.inside(spec.path))
        lines.append(f"Program: {ScriptExecution.select(impl.language, content.decode(errors='replace'), spec).description}")
        listing = files.listing(spec.path)
    except (OSError, PermissionError) as error:
        lines.append(f"On this machine: cannot read it ({error})")
        return "\n".join(lines)
    lines.append("On this machine: same content as picked" if listing.digest == spec.file_digest else f"On this machine: CHANGED since it was picked (sha256 now {listing.digest}); approving would not let it run")
    if listing.git is not None:
        lines.append(f"Git: {listing.git.repository or 'local repository'} at {listing.git.commit[:12]}{'' if listing.git.clean else ', edited since that commit'}")
    return "\n".join(lines)
