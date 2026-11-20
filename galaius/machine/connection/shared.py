"""State the runner creates and more than one trait reads, declared once (data only).

A trait declares what it alone owns, and inherits the class that owns what it calls: `MachineFile` (the machine
file and its audit log), `Events` (numbered events), `Runs` (agent runs and `busy`). `tests/machine/test_traits.py`
fails on a `self.` attribute a trait neither declares nor inherits."""

from pathlib import Path
from uuid import UUID
from galaius.machine.agents import LogRing
from galaius.machine.programs import AgentPrograms
from galaius.machine.places.place_reviews import PlaceReviews
from galaius.upgrade.quiet import QuietPoint


class Shared:
    """Declarations only: no trait creates these, the runner does."""

    config_path: Path
    #: Next event sequence per command in flight: every event of one command, from any path, is ordered.
    _sequences: dict[UUID, int]
    #: The runner's recent log lines (what an error report keeps) and the secrets to mask in them.
    _log_ring: LogRing
    #: Staged writes waiting for the owner's review.
    reviews: PlaceReviews
    #: Set when supervised: leave for a new runtime once nothing runs (`_leave_when_quiet`).
    _quiet: QuietPoint | None
    #: Why the last web version of the agent settings was not applied, and which one (shown on the PC's page).
    _settings_detail: str
    _settings_refused: int
    #: Tokens this runner held before a swap (`take_token_swap`): still masked in every kept line.
    _spent_tokens: tuple[str, ...]
    #: Commands being executed now (0 or 1: one worker runs them in order).
    _executing: int
    #: The agent programs the owner installs from the web, and their sign-in (`AgentPrograms`).
    programs: AgentPrograms
