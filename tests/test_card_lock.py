"""A run's card is drawn while that run's record lock is held — by its dispatcher starting its turn (`agent_queue.dispatch`)
and by the PC's follow-ups (`FollowUps._claim`) — so drawing it must never take that lock itself.

2026-10-26 09:35: the card of the owner's coordinator conversation asked, for each run that had asked it something before
this PC carried reports, whether a turn had answered that ask (`relations._answered_before` → `ask_lines`), reading the run's
queue through `agent_queue.items`, under the run's lock. Its dispatcher already held that lock: the thread waited on itself,
and the conversation answered nothing for 45 minutes."""

import contextlib
import time

from galaius.agents import relations
from galaius.agents import registry as reg
from tests.support import register_run

#: Run ids are UUIDs: only those count as askers (`registry.note_asker_locked`).
LEAD, ASKER = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def test_drawing_a_runs_card_never_takes_that_runs_lock(monkeypatch):
    """A run owed an ask that predates the carrying of reports — the one case the card reads its queue for — is carded
    without its record lock being taken for it."""
    now = time.time()
    register_run(LEAD, name="main", agent="main", task="lead the work", session_id="s1", parent_run_id=None)
    register_run(ASKER, name="reviewer", agent="tester", task="review", session_id="s1", parent_run_id=None)
    with reg.record_lock(LEAD):
        reg.note_asker_locked(LEAD, ASKER)
    reg.record_message_event(from_run=ASKER, to_run=LEAD, text="is it done?", event_id="m-old")
    # Reports were carried only from after that ask: whether a turn answered it is read from the run's queue.
    monkeypatch.setattr(relations, "handback_since", lambda **_: now + 3600)

    taken: list[str] = []
    original = reg.record_lock

    @contextlib.contextmanager
    def watched(run_id: str):
        taken.append(run_id)
        with original(run_id):
            yield

    monkeypatch.setattr(reg, "record_lock", watched)
    relations.card(reg.get_run(LEAD))

    assert LEAD not in taken, "the card took the lock of the run it is drawn for, under which its callers draw it"
