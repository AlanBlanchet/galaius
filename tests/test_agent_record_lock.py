"""A run's record lock (`registry.record_lock`) wedged never takes the whole conversation down with it.

2026-10-26 09:35: the dispatcher of the owner's main conversation took that run's lock and never let it go. From then on
every operation on the run waited on it forever: his message of 09:35 was queued, then `galaius agents send` sat in
`wait_for_start` reading the queue under the lock until the server's 60 s command limit killed it (« Refusé : send timed
out after 60 seconds »); his message of 09:39 never got the lock at all; a worker thread of the machine service and a
`galaius agents list` waited beside it for half an hour; the page said his PC was « hors ligne » though its channel
never dropped."""

import threading
import time

import pytest

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from tests.test_agent_queue import _setup


def _held_elsewhere(run_id: str, seconds: float) -> threading.Thread:
    """Another thread holds `run_id`'s record lock for `seconds` (a dispatcher wedged on it)."""
    taken = threading.Event()

    def hold() -> None:
        with reg.record_lock(run_id):
            taken.set()
            time.sleep(seconds)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert taken.wait(5)
    return holder


def _finishes_within(seconds: float, work) -> tuple[bool, object]:
    """Whether `work()` returns within `seconds`, and what it returned."""
    out: list[object] = []
    worker = threading.Thread(target=lambda: out.append(work()), daemon=True)
    worker.start()
    worker.join(seconds)
    return (not worker.is_alive()), (out[0] if out else None)


def test_a_send_already_queued_answers_while_its_run_is_locked(monkeypatch):
    """Whether its turn started is read, never waited for: the queue file is replaced whole, so reading it needs no lock.
    Its message queued, the send answers within its own bound however long another holder keeps the run."""
    _setup(monkeypatch)
    delivery = messaging.queue_message("r1", "hello", sender="operator")
    _held_elsewhere("r1", 30)

    done, refused = _finishes_within(5, lambda: messaging.wait_for_start(delivery, timeout=1))

    assert done, "wait_for_start waited on the run's record lock"
    assert refused is None   # still queued: its dispatcher delivers it


def test_the_thread_holding_a_runs_lock_may_take_it_again():
    """A nested `record_lock` of the same run in the same thread re-enters: before, it opened the lock file a second time
    and `flock` on that second descriptor waited on the first, for good (one process, one thread, deadlocked)."""
    def nested() -> bool:
        with reg.record_lock("r9"):
            with reg.record_lock("r9"):
                return True

    done, result = _finishes_within(5, nested)

    assert done and result is True


def test_a_persons_send_to_a_locked_run_says_so_within_its_bound(monkeypatch):
    """A person's send waits for the run's lock a bounded time (`messaging.PERSON_PATIENCE`), then says the run is held
    and by whom — never left to the caller's own limit to cut it off without a word."""
    _setup(monkeypatch)
    monkeypatch.setattr(messaging, "PERSON_PATIENCE", 1.0)
    _held_elsewhere("r1", 30)

    def send():
        try:
            messaging.queue_message("r1", "hello", sender="operator")
        except reg.RecordBusy as busy:
            return busy
        return None

    done, busy = _finishes_within(8, send)

    assert done, "the send waited on the run's record lock past its patience"
    assert isinstance(busy, reg.RecordBusy) and "r1" in str(busy)


def test_an_agents_message_keeps_waiting_for_a_busy_run(monkeypatch):
    """Only a person is answered early: an agent's message to a run another holder keeps busy for a while still goes
    through once the lock frees (a bound there would drop reports between agents on any slow turn)."""
    _setup(monkeypatch)
    monkeypatch.setattr(messaging, "PERSON_PATIENCE", 0.2)
    reg.register(run_id="peer", pid=999990, provider=reg.get_run("r1").provider, name="peer", task="p", agent="tester",
                 provider_session_id="v-peer")
    _held_elsewhere("r1", 1.0)

    done, delivery = _finishes_within(8, lambda: messaging.queue_message("r1", "report", sender="peer"))

    assert done and delivery.state == "queued"


def test_a_lock_waited_on_too_long_dumps_every_thread_of_the_waiting_process(monkeypatch):
    """A lock still not taken after `WEDGE_SECONDS` writes, once, every thread's stack of the process waiting for it and
    the processes holding it beside the run's files: the next wedge names the call that holds it without attaching a
    debugger (2026-10-26: the holder could only be read by `ptrace`, which this PC refuses)."""
    monkeypatch.setattr(reg, "WEDGE_SECONDS", 0.5)
    _held_elsewhere("r2", 3)

    with reg.record_lock("r2"):
        pass

    dump = reg.wedge_path("r2").read_text()
    assert "hold" in dump and "record_lock" in dump   # the holding thread's own frame is in it
