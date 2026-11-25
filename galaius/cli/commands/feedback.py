"""`galaius report`: record a problem or an idea about galaius, privately on this computer."""

from typing import Annotated
from cyclopts import Parameter
from galaius.diagnostics import feedback


def report(
    title: str,
    body: str,
    kind: Annotated[str, Parameter(name=["--kind", "-k"])] = "bug",
    public: Annotated[bool, Parameter(name="--public", negative="")] = False,
) -> None:
    """Report a problem or idea about galaius itself: recorded privately on this computer, or filed as a
    public GitHub issue with ``--public``.

    The shell twin of the MCP ``report_issue`` tool — any agent or user with a terminal can
    send feedback without an MCP connection: ``galaius report "title" "what happened"``.

    Parameters
    ----------
    title
        One-line summary of the problem or request.
    body
        What happened, what you expected, any repro steps or environment details.
    kind
        bug | limitation | feedback.
    public
        File it on the public issue tracker; without it the report stays in a private file here.
    """

    print(feedback.report(title, body, kind, public))
