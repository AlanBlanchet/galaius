"""CLI for enrolling and keeping this machine connected to Galaius."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from cyclopts import App, Parameter

from galaius_core import PLACE_LEVELS, AgentTouchScope, MachinePlaceChange, PlaceLevel, WorkflowNode

from galaius.functions import PermissionLevel
from galaius.machine_service import MACHINE_SERVICE, ServiceUnavailable
from galaius.machine_places import PlaceDesk
from galaius.places import split
from galaius.machines import SCRIPT_RUNTIMES, MachineFiles, MachineRunner, ScriptExecution, connect_command

machine_app = App(name="machine", help="Connect this computer as a workflow machine.")


@machine_app.command(name="connect")
def machine_connect(
    server_url: Annotated[str | None, Parameter(name="--server")] = None,
    workspace_id: Annotated[UUID | None, Parameter(name="--workspace")] = None,
    machine_id: Annotated[UUID | None, Parameter(name="--machine")] = None,
    permission_ceiling: PermissionLevel = "read_only",
    working_directory: Annotated[Path | None, Parameter(name="--working-directory")] = None,
    configure_only: bool = False,
) -> None:
    """Save a UI-issued machine credential and connect outbound to its server."""
    asyncio.run(connect_command(server_url, workspace_id, machine_id, None, permission_ceiling, working_directory, not configure_only))


@machine_app.command(name="service")
def machine_service(action: Literal["status", "start", "stop", "restart", "run"] = "status") -> None:
    """The background service keeping this computer connected after `galaius login` (Linux: a
    systemd user unit; macOS: a launchd agent; Windows: a task started at your logon). status (default)
    says whether it runs and where its log is; start / stop / restart act on it; run is what the
    Windows task itself starts."""
    try:
        if action == "run":
            MACHINE_SERVICE.run()
            return
        if action in {"stop", "restart"}:
            MACHINE_SERVICE.stop()
        if action in {"start", "restart"}:
            MACHINE_SERVICE.start()
    except ServiceUnavailable as error:
        print(f"galaius machine service: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    state = "running" if MACHINE_SERVICE.running() else "stopped" if MACHINE_SERVICE.installed() else "not set up (run: galaius login)"
    print(f"Background service: {state}. Log: {MACHINE_SERVICE.logs}")


@machine_app.command(name="places")
def machine_places(path: str | None = None, level: PlaceLevel | None = None) -> None:
    """Owner-only, on this machine: how far each folder (relative to its working directory) is
    open to workflows, the Data screen and agents — hidden (every folder not named), see (names,
    sizes, dates), read, write_on_review (writes wait for your review: `galaius machine
    reviews`), sandbox (read + write, apart from agent and script folders), write. A level holds
    for everything beneath until a deeper one. Set here, it applies at once. No argument prints
    the levels, the widenings the web asked for (confirm them with `galaius machine approve`),
    and whether agents are fenced."""
    runner = MachineRunner()
    desk = PlaceDesk(runner)
    if path is not None:
        if level is None:
            raise SystemExit("name the level: " + ", ".join(PLACE_LEVELS))
        try:
            desk.set_here(path, level)
        except PermissionError as error:
            raise SystemExit(f"galaius machine places: {error}") from None
    config = runner.load()
    print(desk.view(config).model_dump_json(indent=2))


@machine_app.command(name="approve")
def machine_approve(change: str | None = None, *, yes: bool = False) -> None:
    """Owner-only, on this machine: confirm the widenings asked from the web (a folder opened
    further, a new sandbox), each shown first; `change` (an id or its first characters) picks one.
    Nothing asked from the web opens a folder further until confirmed here. --yes confirms the
    one named without asking (never all: one queued after you last looked would pass unseen)."""
    if yes and not change:
        raise SystemExit("name the widening to confirm with --yes (its id, from `galaius machine places`); without --yes each one is shown and asked")
    def confirm(waiting: MachinePlaceChange) -> bool:
        print(f"The web asks: {waiting.path}: {waiting.previous} -> {waiting.level} (asked {waiting.asked_at:%Y-%m-%d %H:%M} UTC, id {str(waiting.id)[:8]}, digest {waiting.digest[:16]})")
        if yes:
            return True
        if not sys.stdin.isatty():
            raise SystemExit("confirm in a terminal, or pass --yes")
        return input("Open it? [y/N] ").strip().lower() in {"y", "yes"}
    applied = PlaceDesk(MachineRunner()).approve(change, confirm)
    print(json.dumps({"applied": [{"path": item.path, "level": item.level, "digest": item.digest} for item in applied]}))


@machine_app.command(name="browse")
def machine_browse(state: Literal["on", "off"] | None = None) -> None:
    """Owner-only, on this machine: whether you may browse the names of every folder here from the
    web to pick levels (names only, never contents; credential stores never listed; off by
    default). No argument prints it."""
    runner = MachineRunner()
    config = runner.update(lambda current: current.model_copy(update={"browse": state == "on"})) if state else runner.load()
    if state:
        PlaceDesk(runner)._log("browse_switch", state=state, method="pc")
    print(json.dumps({"browse": config.browse}))


@machine_app.command(name="fence")
def machine_fence(state: Literal["on", "off"] | None = None) -> None:
    """Owner-only, on this machine: whether agents started here (from the web, or a workflow's
    agent step) run inside an OS fence built from the levels: they see only folders set to read or
    later, and cannot write below write. On, an agent that cannot be fenced is not started
    (sessions are not fenced yet). Linux only (bubblewrap + Landlock). No argument prints whether
    it is on and whether this machine can build it."""
    runner = MachineRunner()
    config = runner.update(lambda current: current.model_copy(update={"fence_agents": state == "on"})) if state else runner.load()
    if state:
        PlaceDesk(runner)._log("fence_switch", state=state, method="pc")
    print(PlaceDesk(runner).view(config).fence.model_dump_json())


@machine_app.command(name="reviews")
def machine_reviews() -> None:
    """Owner-only, on this machine: the writes waiting for your review (agents and workflows
    writing into a write_on_review folder), each with its files and digest."""
    for review in MachineRunner().reviews.list():
        print(f"{str(review.id)[:8]}  {review.place}  {review.origin}  {review.state}  {len(review.files)} file(s)  digest {review.digest}" + (f"  ({review.reason})" if review.reason else ""))


@machine_app.command(name="review")
def machine_review(review: str, *, accept: str | None = None, discard: bool = False) -> None:
    """Owner-only, on this machine: show one review's diff and digest (`review`: its id or first
    characters). --accept DIGEST applies exactly that diff (the digest you read; at least its first
    12 characters); in a terminal you are asked instead. --discard drops it; nothing is written."""
    runner = MachineRunner()
    desk, config = PlaceDesk(runner), runner.load()
    matches = [item for item in runner.reviews.list() if str(item.id).startswith(review)]
    if len(matches) != 1:
        raise SystemExit("no single review starts with that id: see `galaius machine reviews`")
    found = matches[0]
    if discard:
        runner.reviews.discard(found.id)
        desk._log("review", review_id=found.id, place=found.place, outcome="discarded", method="pc", digest=found.digest)
        print(json.dumps({"discarded": str(found.id)}))
        return
    manifest = runner.reviews.manifest(found.id)
    _, lines = runner.reviews.read(found.id, config.place_map().base.joinpath(*split(found.place)), limit=None)
    print("Files (what an editor, git or an agent runs later comes first):")
    print("\n".join(f"  {change:8} {key}" for key, change, _, _ in manifest.entries))
    print("\n".join(lines))
    print(f"\n{found.place}: {len(manifest.entries)} file(s), {found.state}{' (' + found.reason + ')' if found.reason else ''}\ndigest {found.digest}")
    if accept is None and sys.stdin.isatty() and found.state == "ready":
        accept = found.digest if input("Apply exactly this diff? [y/N] ").strip().lower() in {"y", "yes"} else None
    if accept is not None:
        if len(accept) < 12 or not found.digest or not found.digest.startswith(accept):
            raise SystemExit("that is not this review's digest (read it again above)")
        try:
            desk.accept(found.id, found.digest)
        except PermissionError as error:
            raise SystemExit(f"galaius machine review: {error}") from None
        print(json.dumps({"accepted": str(found.id), "digest": found.digest}))


@machine_app.command(name="permission")
def machine_permission(ceiling: PermissionLevel | None = None) -> None:
    """Owner-only, on this machine: the most a workflow step may do here. read_only (what
    `galaius login` sets): file reads, models, functions marked read-only; full_access: also
    Script steps (each still needs your approval of its exact code) and file writes. No argument
    prints it; the server can never raise it."""
    runner = MachineRunner()
    config = runner.update(lambda current: current.model_copy(update={"permission_ceiling": ceiling})) if ceiling else runner.load()
    print(json.dumps({"permission_ceiling": config.permission_ceiling}))


@machine_app.command(name="agents")
def machine_agents(state: Literal["on", "off"] | None = None, *,
                   continue_conversations: Annotated[Literal["on", "off"] | None, Parameter(name="--continue")] = None,
                   answer_approvals: Annotated[Literal["on", "off"] | None, Parameter(name="--approvals")] = None) -> None:
    """Owner-only, on this machine: whether agents may run here (workflow agent steps and agents
    started from the web; an agent CLI can read any file of this user). --continue: the web may
    continue your editor conversations here (as a copy). --approvals: the web may answer what a
    session asks before running a command or changing a file. No argument prints the settings."""
    changes = {key: value == "on" for key, value in (("run_agents", state), ("continue_conversations", continue_conversations), ("answer_approvals", answer_approvals)) if value is not None}
    runner = MachineRunner()
    config = runner.update(lambda current: current.model_copy(update=changes)) if changes else runner.load()
    print(json.dumps({"run_agents": config.run_agents, "continue_conversations": config.continue_conversations, "answer_approvals": config.answer_approvals}))


@machine_app.command(name="remote")
def machine_remote(state: Literal["on", "off"] | None = None) -> None:
    """Owner-only, on this machine: whether its page on the web may change its agent settings
    (agents on/off, agent folders, read_only / workspace_write, the two opt-ins, the repositories
    it may clone). off is this computer's kill switch: nothing on the server can turn it back on,
    and agent settings then change only here. No argument prints it."""
    runner = MachineRunner()
    config = runner.update(lambda current: current.model_copy(update={"remote_settings": state == "on"})) if state is not None else runner.load()
    print(json.dumps({"remote_settings": config.remote_settings, "settings_revision": config.settings_revision, "web_settings_version": config.web_settings_version}))


@machine_app.command(name="script-roots")
def machine_script_roots(*roots: str) -> None:
    """Owner-only, on this machine: the folders (relative to its working directory) Script steps
    may run a file from. Never inside or around a folder anything writes in, so no workflow can write beside a
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
    agents in from the web, in any folder beneath them. Never inside or around a sandbox or a
    script root. No argument prints them; arguments replace them; "" alone clears them."""
    runner = MachineRunner()
    config = runner.load()
    if roots:
        config = runner.update(lambda current: current.model_copy(update={"agent_roots": tuple(root for root in roots if root)}))
    usable, refused = config.usable_agent_roots()
    base = config.working_directory.resolve()
    print(json.dumps({"working_directory": str(config.working_directory), "agent_roots": [root.relative_to(base).as_posix() for root in usable], "refused": list(refused),
                      "agent_permission": config.agent_permission, "run_agents": config.run_agents}))


@machine_app.command(name="agent-permission")
def machine_agent_permission(scope: AgentTouchScope | None = None) -> None:
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
    from galaius.server_workspace import ServerWorkspace
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
    language = SCRIPT_RUNTIMES[impl.language].title
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
        lines.append("Program: " + (ScriptExecution.select(impl.language, "", spec).description if spec.interpreter or impl.language != "python" else "not checked here; python3 or uv, depending on the file's declared packages"))
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
