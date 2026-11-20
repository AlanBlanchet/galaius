"""What this computer says of itself when it cannot connect: the problem its page shows and the error report draft its owner may send."""

import httpx
from galaius_core import MachineProblem, MachineProblemCode
from galaius.diagnostics.error_reports import MachineErrorReports
from galaius.diagnostics.redaction import redact
from galaius.host.paths import UserPaths
from galaius_core.wire import readable
from galaius.machine.connection.config import MachineConfig


def report(config: MachineConfig, code: MachineProblemCode, detail: str = "", lines: tuple[str, ...] = (), secrets: tuple[str, ...] = ()) -> bool:
    """Tells its server why this computer is not connected (`MachineProblem`, shown on its page):
    over HTTPS with its machine token, which still answers when the channel does not. Best
    effort, credentials masked: False when the server did not take it. The same problem also
    prepares an error report from `lines` (what led to it) and asks its owner on that page
    whether to send it (`error_reports`): nothing of the lines leaves before his yes."""
    # What a person can read of `detail` and no more: a service log's end is this program's own text as
    # often as a sentence (« KeyError: 'token' »), and its page shows these words as this computer's own
    # (visual-critic 2026-10-15). The whole text stays in this computer's log and in `lines`, which goes
    # nowhere before its owner says so.
    problem = MachineProblem(code=code, detail=readable(redact(detail, (config.token.get_secret_value(),)).strip())[-500:])
    # The question's line is galaius's own last line, never `detail`: on an install it is the service
    # log's raw end, which a program the PC ran may have written (threat model S1). Never raises.
    drafts().prepare(config, code, lines[-1] if lines else code, lines, secrets)
    try:
        response = httpx.post(config.endpoint("/v1/machine/problem"), json=problem.model_dump(mode="json", exclude_none=True), headers=config.authorization, timeout=10)
    except httpx.HTTPError:
        return False
    return response.status_code == 204


def drafts() -> MachineErrorReports:
    """This computer's error report drafts, beside its machine file."""
    return MachineErrorReports(folder=UserPaths.config() / "error-reports")
