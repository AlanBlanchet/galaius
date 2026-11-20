"""What the runner's traits share: the state and knobs more than one of them reads, declared once.

Each trait (`Answers`, `Commands`, `Runs`, `Upkeep`, `Watches`) declares what it alone owns in its own class;
`MachineRunner` creates the values (`__init__`) and owns the connection itself. `tests/machine/test_traits.py`
fails on a `self.` attribute that none of them declares."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID
from galaius.machine.agents import LogRing
from galaius.machine.places.place_reviews import PlaceReviews
from galaius.upgrade.quiet import QuietPoint
from galaius.machine.connection.logs import audit


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

    #: How often the usage lines of runs pinned to a subscription are read (caps applied) and moved
    #: usage goes to the server (`SubscriptionWatch`).
    subscription_seconds = 5
    #: Appends one line to an owner-only log of this PC (`logs.audit`): log name, entry.
    audit: Callable[[str, dict[str, object]], None] = staticmethod(audit)
