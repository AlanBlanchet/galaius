"""Trait: one command from the server, accepted, run and reported."""

import asyncio
import hashlib
import json
import logging
import os
import time
import subprocess
import tempfile
from pathlib import Path
from typing import ClassVar
from uuid import UUID
from galaius_core import MACHINE_MODELS, EgressAllowEntry, EgressPolicy, MachineCommand, MachineCommandResult, ScriptFile, ScriptLanguage, UserModelOrigin
from galaius_core.wire import fitted
from galaius.workflows.functions import FunctionRegistry, invoke as invoke_function
from galaius.inference.env import Report, ensure_speech_env, ensure_vision_env
from galaius.isolation import gpu_scrub
from galaius.inference import user_models
from galaius.isolation.sandbox import run_pooled
from galaius.host.windowless import console_python

from galaius.machine.connection import inventory, signing
from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.faults import said_of
from galaius.machine.connection.files import CommandFiles, MachineFiles
from galaius.machine.connection.logs import CommandLogs
from galaius.inference.env import ModelWorker
from galaius.machine.connection.runs import Runs
from galaius.machine.connection.script import SCRIPT_RUNTIMES, ScriptExecution, ScriptRuntime, audit_script

logger = logging.getLogger(__package__)


class Commands(Runs):
    """How the runner executes a command: model, speech, function or script."""

    #: The model runtimes kept loaded between steps, by script (`MachineConfig.model_keep_warm_seconds`).
    _workers: dict[str, ModelWorker]

    def _accept_command(self, config: MachineConfig, connected: MachineConfig, command: MachineCommand) -> MachineConfig:
        self._same_enrollment(config, connected)
        if command.machine.id != config.machine_id:
            raise PermissionError("machine command targets another machine")
        # A pooled command legitimately carries a DIFFERENT workspace_id (the TENANT borrowing
        # this machine) than `config.workspace_id` (this machine's OWNER) — the HMAC signature
        # below, keyed on this machine's own token, is what proves the OWNER's server authorized
        # it; the equality check stays absolute for every non-pooled command (today's 1:1 shape,
        # unchanged: threat-model-shared-compute-2026-09-24.md threat #7's invariant).
        if command.tenancy == "owner" and command.workspace_id != config.workspace_id:
            raise PermissionError("machine command targets another workspace")
        if command.tenancy == "pooled" and command.impl.kind != "script":
            # Function/model pooled dispatch needs its own sandboxed fetch/invoke path
            # (galaius.inference.weights's safetensors gate, a sandboxed function call) — not built
            # yet; refuse rather than silently run either unsandboxed or not at all.
            raise PermissionError(f"pooled dispatch of a {command.impl.kind!r} step is not supported yet; script only")
        signing.verify(config, command, "machine command")
        if command.nonce in config.seen_nonces:
            raise PermissionError("machine command nonce was already used")
        return config.model_copy(update={"seen_nonces": (*config.seen_nonces[-9998:], command.nonce)})

    async def _execute(self, socket, config: MachineConfig, command: MachineCommand) -> None:
        config = await asyncio.to_thread(self.update, lambda current: self._accept_command(current, config, command))
        bound = {"machine_id": config.machine_id, "command_id": command.id, "run_id": command.run_id, "node_id": command.node_id, "action": command.impl.kind}
        # Everything the runner logs from here to the result reaches the server inside this step.
        forwarded = CommandLogs()
        package = logging.getLogger("galaius")
        package.addHandler(forwarded)
        logger.info("running %s step", command.impl.kind, extra=bound)
        await self._event(socket, command.id, "started")
        started = time.perf_counter()
        try:
            try:
                if command.tenancy == "pooled":
                    output = json.dumps(await asyncio.to_thread(self._run_script_pooled, command, config), separators=(",", ":"))
                elif command.impl.kind == "agent":
                    if not config.run_agents:
                        raise PermissionError("agent steps are off on this computer; its owner turns them on there with `galaius machine agents on`")
                    output = await self._run_agent(command.impl.agent, str(command.inputs["task"]), config, socket, command.id)
                else:
                    # Files first: a step never sees a server reference, only a checked local copy.
                    files = CommandFiles(config=config, command=command)
                    try:
                        files = await asyncio.to_thread(files.fetch_inputs)
                        if command.impl.kind == "builtin":
                            answer = await asyncio.to_thread(files.write if command.impl.op == "write_artifact" else files.read)
                        elif command.impl.kind == "model":
                            answer = await asyncio.to_thread(self._run_model, files.command, config, self._reporter(socket, command.id))
                        else:
                            answer = await asyncio.to_thread({"function": self._run_function, "script": self._run_script}[command.impl.kind], files.command, config)
                    finally:
                        files.discard()
                    output = json.dumps(answer, separators=(",", ":"))
                result = MachineCommandResult(command_id=command.id, nonce=command.nonce, status="succeeded", result=output[:256 * 1024])
            except Exception as error:
                logger.error("%s step failed", command.impl.kind, exc_info=error, extra=bound)
                result = MachineCommandResult(command_id=command.id, nonce=command.nonce, status="failed", error=fitted(MachineCommandResult, "error", said_of(error, doing="running_step").said))
        finally:
            # Threat #1c: a pooled tenant's VRAM must be scrubbed whether its run succeeded,
            # failed, or crashed — never conditioned on the run's own outcome.
            if command.tenancy == "pooled":
                await self._scrub_gpu_if_present(socket, command.id)
        duration = round((time.perf_counter() - started) * 1000)
        logger.info("%s step %s in %d ms", command.impl.kind, result.status, duration, extra={**bound, "status": result.status, "duration_ms": duration})
        package.removeHandler(forwarded)
        try:
            for line in forwarded.drain():
                await self._event(socket, command.id, "progress", line)
            await socket.send(json.dumps({"type": "result", "result": result.model_dump(mode="json")}))
        finally:
            self._sequences.pop(command.id, None)

    def _reporter(self, socket, command_id: UUID) -> Report:
        """What a step running on a worker thread uses to tell the server its phase (a `phase`
        progress event, `galaius_core.MACHINE_PHASES`); never fails the step."""
        loop = asyncio.get_running_loop()

        def report(phase: str, detail: str) -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._event(socket, command_id, "progress", {"kind": "phase", "phase": phase, "detail": detail[:240]}), loop).result(timeout=10)
            except Exception:
                logger.warning("step phase not sent", exc_info=True)
        return report

    def _run_model(self, command: MachineCommand, config: MachineConfig, report: Report = lambda phase, detail: None) -> dict[str, object]:
        """Run one registered model on paths inside the machine's approved workspace: the vendor
        catalog (`galaius_core.MACHINE_MODELS`, `provider == "huggingface"`) below, or — the
        whole point of "run inferences on any kind of model" — a workspace's OWN registered model
        (`provider == "workspace"`, `_run_user_model`), dispatched first since its `impl.model` is
        a `UserModel` UUID, never a `MACHINE_MODELS` key.

        Inference itself runs in `vision_infer.py`, inside the isolated venv `ensure_vision_env`
        provisions under `~/.galaius/models/env` — torch and transformers never load into this
        process, so a machine with no vision node configured never pays for them."""
        if command.impl.provider == "workspace":
            return self._run_user_model(command, config, report)
        model_id = command.impl.model
        if MACHINE_MODELS[model_id].task == "automatic-speech-recognition":
            return self._run_speech(command, config, report)
        images = command.inputs.get("images", command.config.get("images", ()))
        score_threshold = command.config.get("score_threshold", 0.5)
        # The one registry (galaius_core.MACHINE_MODELS); the runner script names tasks its own way.
        spec = MACHINE_MODELS[model_id]
        task_name, license_name = {"object-detection": "detection", "image-segmentation": "segmentation"}[spec.task], spec.license
        cache_root = Path.home() / ".galaius" / "models"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_root.chmod(0o700)

        run_dir = cache_root / "runs" / str(command.run_id) / str(command.node_id)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.chmod(0o700)
        image_paths = [str(self._model_input_path(raw_path, config.working_directory)) for raw_path in (images if isinstance(images, list) else [images])]

        logger.info("%s (%s) on %d image(s), score threshold %s", model_id, spec.task, len(image_paths), score_threshold)
        python = ensure_vision_env(cache_root, report=report)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps({
            "model": model_id,
            "task": task_name,
            "license": license_name,
            "cache_root": str(cache_root),
            "run_dir": str(run_dir),
            "score_threshold": score_threshold,
            "image_paths": image_paths,
        }), encoding="utf-8")
        output = self._worker("vision_infer.py", config).run(python, request_path, report)
        found = {key: len(value) for key, value in output.items() if isinstance(value, list)} if isinstance(output, dict) else {}
        logger.info("%s answered: %s", model_id, ", ".join(f"{count} {key}" for key, count in found.items()) or "no list output")
        return output

    #: The languages the owner speaks to the blob: Whisper tells them apart before transcribing, and
    #: a third language it half-hears never wins (`speech_infer.spoken_language`).
    SPOKEN_LANGUAGES: ClassVar[tuple[str, ...]] = ("fr", "en")

    def _run_speech(self, command: MachineCommand, config: MachineConfig, report: Report) -> dict[str, object]:
        """One recording to text on this machine's own GPU (`speech_infer.py`, kept warm): the
        recording is the command's `audio` input, already a checked local copy (`CommandFiles`)."""
        cache_root = Path.home() / ".galaius" / "models"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = cache_root / "runs" / str(command.run_id) / str(command.node_id)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        python = ensure_speech_env(cache_root, report=report)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps({"model": command.impl.model, "audio_path": str(command.inputs["audio"]), "cache_root": str(cache_root),
                                            "languages": list(self.SPOKEN_LANGUAGES), "hotwords": str(command.config.get("vocabulary") or ""),
                                            "hint": str(command.config.get("vocabulary_hint") or "on_mishear")}), encoding="utf-8")
        output = self._worker("speech_infer.py", config).run(python, request_path, report)
        logger.info("%s heard %s s of %s", command.impl.model, output.get("seconds"), output.get("language"))
        return output

    def _run_user_model(self, command: MachineCommand, config: MachineConfig, report: Report = lambda phase, detail: None) -> dict[str, object]:
        """Run one workspace-registered model (`galaius_core.UserModel`, `impl.provider ==
        "workspace"`): the server resolved its CURRENT `origin` at dispatch time and signed it
        into `command.config["_user_model"]` (never stale — a later edit to the registration is read
        on the NEXT run, same as every other resource this app resolves at dispatch time). This machine
        independently re-validates that shape (`UserModelOrigin.model_validate`, never a bare dict
        trusted as-is), fetches the weights (`galaius.inference.user_models`, byte-verified safetensors-only
        inside that call) and only then hands a LOCAL path to the same isolated `vision_env`
        subprocess the vendor catalog models use above — origin bytes never touch this process,
        torch/transformers still never import here.

        Two capability boundaries this machine refuses past, by name, rather than pretending to
        cross them: a `docker_image` origin is pulled (real, useful — proves the image is still
        reachable) but never RUN (no proven container I/O contract, no verified GPU passthrough);
        a `trusts_remote_code` origin is fetched and byte-verified but never LOADED (loading it
        would execute the repo's own Python, which this machine only sandboxes at the same gVisor
        tier a script node gets — unverified for GPU passthrough on this build, threat-model
        `pool.py` docstring). Both refusals happen AFTER their real, safe half of the work, never
        instead of it."""
        payload = command.config.get("_user_model")
        if not isinstance(payload, dict) or not isinstance(payload.get("origin"), dict):
            raise RuntimeError("server did not resolve this workspace model's origin; reconnect and re-run")
        origin = UserModelOrigin.model_validate(payload["origin"])
        licence = str(payload.get("licence") or "")
        task = command.impl.task
        if task not in user_models.SUPPORTED_TASKS:
            raise RuntimeError(f"this machine's runner does not support task {task!r} yet (supported: {', '.join(sorted(user_models.SUPPORTED_TASKS))})")
        cache_root = Path.home() / ".galaius" / "models"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_root.chmod(0o700)
        label = f"{origin.repo_id} @ {origin.revision or 'main'}" if origin.repo_id else command.impl.model
        if origin.kind == "docker_image":
            pulled = user_models.pull_docker_image(origin)
            raise RuntimeError(f"pulled and verified {pulled}; running a Docker-image model is not implemented on this machine yet")
        logger.info("fetching workspace model %s (%s) from %s", label, task, origin.kind)
        weights_dir = user_models.fetch_weights_dir(origin, command.impl.model, cache_root, config.server_url, config.token.get_secret_value())
        if origin.trusts_remote_code:
            raise RuntimeError(f"fetched and byte-verified {label}'s weights, but this machine has no sandboxed runtime for trust_remote_code models yet")

        images = command.inputs.get("images", command.config.get("images", ()))
        score_threshold = command.config.get("score_threshold", 0.5)
        task_name = {"object-detection": "detection", "image-segmentation": "segmentation"}[task]
        run_dir = cache_root / "runs" / str(command.run_id) / str(command.node_id)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.chmod(0o700)
        image_paths = [str(self._model_input_path(raw_path, config.working_directory)) for raw_path in (images if isinstance(images, list) else [images])]

        logger.info("%s (%s) on %d image(s), score threshold %s", label, task, len(image_paths), score_threshold)
        python = ensure_vision_env(cache_root, report=report)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps({
            "model": label,
            "task": task_name,
            "license": licence,
            "cache_root": str(cache_root),
            "run_dir": str(run_dir),
            "score_threshold": score_threshold,
            "image_paths": image_paths,
            "weights_dir": str(weights_dir),
        }), encoding="utf-8")
        output = self._worker("vision_infer.py", config).run(python, request_path, report)
        found = {key: len(value) for key, value in output.items() if isinstance(value, list)} if isinstance(output, dict) else {}
        logger.info("%s answered: %s", label, ", ".join(f"{count} {key}" for key, count in found.items()) or "no list output")
        return output

    def _run_function(self, command: MachineCommand, config: MachineConfig) -> object:
        """Runs one `@galaius.function`-decorated Python callable or registered shell command,
        the inbound half of PC integration (`_run_model`'s sibling: local compute reached FROM a
        workflow). `function_version` is re-checked against the entry's OWN current content hash
        — a workflow node stays pinned to the exact signature it was wired against, so a local
        edit that changes ports fails loudly here instead of silently coercing arguments — and
        the LOCAL permission ceiling still gates a `full_access` function exactly like every
        other machine command."""
        name = command.impl.name
        entry = FunctionRegistry().get(name)
        if entry.version != command.impl.version:
            raise RuntimeError(f"function {name!r} changed on this machine since the workflow was saved; re-select it")
        if entry.permission == "full_access" and config.permission_ceiling != "full_access":
            raise PermissionError(f"function {name!r} needs full-access permission; this machine is read-only")
        # A port's constant (the node's config) first, its wired value over it.
        arguments = {**command.config, **command.inputs}
        logger.info("function %s (%s) with %s", name, entry.version[:8], ", ".join(sorted(arguments)) or "no arguments")
        return invoke_function(entry, arguments, working_directory=config.working_directory)

    def _run_script(self, command: MachineCommand, config: MachineConfig, timeout: float = 120) -> str:
        """Runs a script the machine owner has already approved by its EXACT digest — a server-side
        gate (`MachineStore.script_approved`) checked BEFORE this ever dispatches; re-verified here
        too (defense in depth: two independent checks of the same bytes, never one trusted alone):
        inline source against its digest, a file of the machine (`ScriptFile`) against the sha256
        it was picked with, read NOW (commands run one at a time, so no other step rewrites it
        between this check and the start). Threat-modeler mitigations applied: #2 mandatory
        full_access (arbitrary code exec is de facto full_access regardless of any local setting);
        #4 one audit line per ATTEMPT, written before running, so a crash still leaves a record; #5
        a real subprocess (genuine timeout kill), the same stripped credential-free environment
        `_run_agent` uses, confined to the working directory (a file: its folder or its `cwd`)."""
        language, digest = command.impl.language, command.impl.source_digest
        if config.permission_ceiling != "full_access":
            raise PermissionError("script nodes always need full-access permission; this machine is read-only")
        if command.impl.origin == "machine_file":
            spec = command.impl.script_file(command.config)
            files = MachineFiles(config=self._current_config(config), area="scripts")
            path = files.inside(spec.path)
            content = files.script_bytes(path)
            found = hashlib.sha256(content).hexdigest()
            if found != spec.file_digest:
                raise RuntimeError(f"{spec.path} changed on this machine since it was picked (its sha256 is now {found[:12]}, it was {spec.file_digest[:12]}): check the file again in the step, then its owner approves the new digest")
            audit_script(command, spec.path)
            logger.info("%s script file %s (%s), timeout %s s", language, spec.path, found[:12], timeout)
            argv = [*self._interpreter(language, content.decode(errors="replace"), spec), str(path), *spec.args]
            if any(Path(argv[0]).resolve() == root or root in Path(argv[0]).resolve().parents for root in files.config.writable_folders()):
                raise PermissionError(f"{spec.interpreter}: the program that runs a script cannot live in a folder workflow file steps write to")
            return self._run_process(argv, files.inside(spec.cwd) if spec.cwd else path.parent, timeout)
        source = str(command.config["source"])
        try:
            command.impl.check(command.config)  # the same rule the server applied: source, language and pin agree
        except ValueError as error:
            raise RuntimeError(f"{error}; refusing to run") from error
        audit_script(command)
        logger.info("%s script %s (%d lines), timeout %s s", language, digest[:12], source.count("\n") + 1, timeout)
        with tempfile.TemporaryDirectory(prefix="galaius-script-") as scratch:
            script_path = Path(scratch) / f"script{SCRIPT_RUNTIMES[language].suffix}"
            script_path.write_text(source, encoding="utf-8")
            script_path.chmod(0o700)
            return self._run_process([*self._interpreter(language, source), str(script_path)], config.working_directory, timeout)

    def _run_process(self, argv: list[str], cwd: Path, timeout: float) -> str:
        """One script process: its standard output is the step's result, a non-zero exit its error."""
        try:
            completed = subprocess.run(argv, cwd=cwd, env=inventory.safe_environment(), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"script exceeded its {timeout}s timeout") from error
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or f"script exited {completed.returncode}").strip()[-2000:])
        if completed.stderr.strip():
            logger.warning("script wrote to stderr: %s", completed.stderr.strip()[-600:])
        logger.info("script exited 0, %d bytes of output", len(completed.stdout))
        return completed.stdout

    @classmethod
    def _interpreter(cls, language: ScriptLanguage, source: str, spec: ScriptFile | None = None) -> list[str]:
        """Resolve the same execution description the owner sees before approval; a language this
        computer cannot run is refused in plain words, never run by another program."""
        execution = ScriptExecution.select(language, source, spec)
        path = inventory.safe_environment().get("PATH")
        if execution.program == "custom":
            interpreter = execution.interpreter
            if ("/" in interpreter or "\\" in interpreter) and not Path(interpreter).is_absolute():
                raise RuntimeError(f"{interpreter}: name the program by its full path, or by a name on the machine's PATH")
            found = interpreter if Path(interpreter).is_absolute() else ScriptRuntime.which(interpreter, path)
            if found is None or not os.access(found, os.X_OK):
                raise RuntimeError(f"{interpreter} is not a program this machine can run (not found on its PATH)")
            return [found]
        if execution.program == "runner_python":
            return [console_python()]
        if execution.program == "uv":
            found = ScriptRuntime.which("uv", path)
            if found is None:
                raise RuntimeError("this script declares the packages it needs (PEP 723): install uv on this machine to run it")
            return [found, *execution.options]
        found = execution.runtime.program(path)
        if found is None:
            raise RuntimeError(execution.runtime.missing)
        return [found, *execution.options]

    def _run_script_pooled(self, command: MachineCommand, config: MachineConfig, timeout: float = 120) -> str:
        """The POOLED sibling of `_run_script`: same digest re-check and audit log, but the
        source runs inside a fresh gVisor container (`galaius.isolation.sandbox.run_pooled`) instead of a
        bare subprocess on this host — the tenant did not enroll this machine and never gets its
        real filesystem, PID namespace, or unrestricted network. `_pool_egress_allow` (a plain
        list of `{host,port}` the server stamped into `command.config`) becomes a typed
        `EgressPolicy`; empty means no network at all, the sandbox's own default."""
        language, digest, source = command.impl.language, command.impl.source_digest, str(command.config["source"])
        try:
            command.impl.check(command.config)  # the same rule the server applied: source, language and pin agree
        except ValueError as error:
            raise RuntimeError(f"{error}; refusing to run") from error
        if config.permission_ceiling != "full_access":
            raise PermissionError("script nodes always need full-access permission; this machine is read-only")
        audit_script(command)
        raw_allow = command.config.get("_pool_egress_allow", [])
        policy = EgressPolicy(allow=tuple(EgressAllowEntry.model_validate(entry) for entry in raw_allow)) if isinstance(raw_allow, list) else EgressPolicy()
        runtime = SCRIPT_RUNTIMES[language]
        if runtime.pooled is None:
            raise RuntimeError(f"pooled machines do not run {runtime.title} scripts")
        interpreter, script_name = runtime.pooled, f"script{runtime.suffix}"
        logger.info("pooled %s script %s (%d lines), timeout %s s, egress allow=%d host(s)", language, digest[:12], source.count("\n") + 1, timeout, len(policy.allow))
        result = run_pooled(run_id=command.run_id, command=[interpreter, script_name], input_files={script_name: source.encode()}, egress=policy, timeout=timeout)
        if result.exit_code != 0:
            raise RuntimeError((result.stderr or f"pooled script exited {result.exit_code}").strip()[-2000:])
        if result.stderr.strip():
            logger.warning("pooled script wrote to stderr: %s", result.stderr.strip()[-600:])
        logger.info("pooled script exited 0, %d bytes of output", len(result.stdout))
        return result.stdout

    async def _scrub_gpu_if_present(self, socket, command_id: UUID) -> None:
        """Runs after EVERY pooled command, success or failure — threat #1c: a machine reporting a
        CUDA accelerator gets its free VRAM scrubbed (`galaius.isolation.gpu_scrub`) before the next tenant
        can be placed on it, and the server only trusts a fresh `GpuScrubRecord` from THIS event,
        never from the run having merely succeeded."""
        if not inventory.cuda_accelerators():
            return
        try:
            scrubbed_bytes = await asyncio.to_thread(gpu_scrub.scrub_all_free_memory)
            kind = "device_reset" if gpu_scrub.gpu_reset_supported() else "scrubbed"
            logger.info("pooled GPU scrub: %d bytes, kind=%s", scrubbed_bytes, kind)
            # `kind="gpu_scrub"` here is the PROGRESS PAYLOAD's own content-type tag (like
            # `CommandLogs`' `{"kind": "log", ...}`), never the outer `MachineEvent.kind` (that
            # stays "progress" — `_event`'s third argument, below); the server reads
            # `event.payload["kind"]`, not `event.kind`, to recognise this one.
            await self._event(socket, command_id, "progress", {"kind": "gpu_scrub", "accelerator_index": 0, "scrub_kind": kind})
        except gpu_scrub.CudaUnavailable as error:
            # A machine that REPORTED a CUDA accelerator but cannot actually reach the driver is a
            # machine this server must never trust with the next tenant's GPU placement — logged
            # loudly rather than silently letting `gpu_scrub_fresh` stay stale on the server side.
            logger.error("pooled GPU scrub failed, device may be left dirty for the next tenant: %s", error)

    @staticmethod
    def _model_input_path(raw_path: str, working_directory: Path) -> Path:
        root = working_directory.resolve(strict=True)
        candidate = Path(raw_path)
        image_path = candidate.resolve(strict=True) if candidate.is_absolute() else (root / candidate).resolve(strict=True)
        if not image_path.is_relative_to(root) or not image_path.is_file():
            raise ValueError("model image paths must be files under the machine working directory")
        return image_path

    def _worker(self, script: str, config: MachineConfig) -> ModelWorker:
        worker = self._workers.get(script)
        if worker is None or worker.keep_warm != config.model_keep_warm_seconds:
            if worker is not None:
                worker.close()
            worker = self._workers[script] = ModelWorker(script, config.model_keep_warm_seconds)
        return worker
