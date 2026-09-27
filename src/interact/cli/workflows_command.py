"""`interact workflows run NAME` — start a server workflow from a shell script, follow it, print
its outputs as JSON and exit by its outcome, so the script continues with them:

    summary=$(interact workflows run "Write a report" --input topic=Q3) && echo "$summary" | jq .outputs

Exit status: 0 succeeded (or accepted, with `--detach`), 1 the run ended failed / cancelled /
interrupted, 2 it could not be started, followed or its files saved (unknown workflow, refused,
inputs rejected, server unreachable, `--timeout`, a file failing its digest), 130 interrupted by
Ctrl-C while the run goes on. Progress goes to stderr, the JSON to stdout."""

import json
import sys
from pathlib import Path
from typing import Annotated

import httpx
from cyclopts import App, Parameter

from interact.client import Client
from interact.workflows import Run

workflows_app = App(name="workflows", help="Run server workflows from scripts: wait for the end, print outputs as JSON.")


class WorkflowsCLI:
    #: Everything that means "could not start or follow the run" (exit 2), as opposed to a run
    #: that ended without succeeding (exit 1).
    FAILURES = (LookupError, ValueError, OSError, TimeoutError, httpx.HTTPError)

    @staticmethod
    def inputs(pairs: list[str], input_json: Path | None) -> dict[str, object]:
        """`--input-json` (a file, `-` for stdin) first, then each `--input name=value` on top; a
        value is JSON when it parses (`5`, `true`, `[1,2]`), text otherwise."""
        values = json.loads(sys.stdin.read() if str(input_json) == "-" else input_json.read_text()) if input_json else {}
        if not isinstance(values, dict):
            raise ValueError("--input-json must hold a JSON object of input names")
        for pair in pairs:
            name, separator, raw = pair.partition("=")
            if not separator or not name:
                raise ValueError(f"expected --input name=value, got {pair!r}")
            try:
                values[name] = json.loads(raw)
            except ValueError:
                values[name] = raw
        return values

    @staticmethod
    def follow(run: Run, timeout: float | None, quiet: bool) -> None:
        for event in run.stream(timeout):
            if not quiet and (line := run.describe(event)):
                print(line, file=sys.stderr, flush=True)

    @staticmethod
    def report(run: Run, download: Path | None, overwrite: bool) -> None:
        summary = run.summary()
        if download is not None and run.status == "succeeded":
            download.mkdir(parents=True, exist_ok=True)
            for name, artifact in run.files.items():
                summary["outputs"][name] = {**artifact.model_dump(mode="json"), "downloaded_to": str(run.download(artifact, download, overwrite=overwrite))}
        print(json.dumps(summary, default=str))

    @classmethod
    def execute(cls, operation, timeout: float | None, quiet: bool, download: Path | None, overwrite: bool, detach: bool = False) -> None:
        """Runs `operation` (which returns a `Run`), follows it unless detached, and exits."""
        run = None
        try:
            run = operation()
            if not detach:
                cls.follow(run, timeout, quiet)
            cls.report(run, None if detach else download, overwrite)
        except KeyboardInterrupt:
            print(f"stopped waiting; the run continues server-side: interact workflows wait {run.id}" if run else "interrupted before the run started", file=sys.stderr)
            raise SystemExit(130) from None
        except cls.FAILURES as error:
            print(json.dumps({"run_id": str(run.id) if run else None, "status": run.status if run else None, "error": str(error)}))
            raise SystemExit(2) from None
        raise SystemExit(0 if detach or run.status == "succeeded" else 1)


@workflows_app.command(name="run")
def workflows_run(workflow: str, *, input: Annotated[list[str] | None, Parameter(name="--input")] = None,
                  input_json: Annotated[Path | None, Parameter(allow_leading_hyphen=True)] = None, detach: bool = False, timeout: float | None = None,
                  download: Path | None = None, overwrite: bool = False, quiet: bool = False,
                  idempotency_key: str | None = None, poll_seconds: float = 1.0) -> None:
    """Start WORKFLOW (exact name or id) and wait for its end.

    Parameters
    ----------
    workflow
        Exact workflow name, or its id when several share a name.
    input
        `name=value`, repeatable; the value is JSON when it parses, text otherwise.
    input_json
        File holding a JSON object of inputs (`-` reads stdin); `--input` wins on the same name.
    detach
        Return as soon as the server accepted the run; `interact workflows wait RUN_ID` follows it later.
    timeout
        Seconds to wait before giving up (exit 2); the run itself keeps going.
    download
        Directory to save every file output into, under its relative path (checked against its recorded digest).
    overwrite
        Replace a different file already there; without it that file stops the save (exit 2).
    quiet
        No progress lines on stderr.
    idempotency_key
        Reuse to make a retried script return the run this key already started instead of a new one.
    poll_seconds
        Seconds between progress checks.
    """
    WorkflowsCLI.execute(lambda: Client(poll_seconds=poll_seconds).workflows.start(workflow, WorkflowsCLI.inputs(input or [], input_json), idempotency_key=idempotency_key),
                         timeout, quiet, download, overwrite, detach)


@workflows_app.command(name="wait")
def workflows_wait(run_id: str, *, timeout: float | None = None, download: Path | None = None, overwrite: bool = False, quiet: bool = False, poll_seconds: float = 1.0) -> None:
    """Follow a run started elsewhere (`--detach`, another script, the web app) to its end; same
    output and exit status as `run`."""
    WorkflowsCLI.execute(lambda: Client(poll_seconds=poll_seconds).workflows.attach(run_id), timeout, quiet, download, overwrite)
