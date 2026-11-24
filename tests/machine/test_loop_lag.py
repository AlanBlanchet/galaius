"""A loop that answers nothing is said, so a link dropped for unanswered pings (code 1011) is told from a stalled uplink
(2026-11-23 10:36 UTC: the service log was silent 7 minutes before one)."""

import asyncio
import logging
import time

import pytest

from galaius.machine.connection import runner


@pytest.mark.asyncio
async def test_a_blocked_loop_is_said_with_how_long_and_a_quiet_one_is_not(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(runner, "LOOP_LAG_SECONDS", 0.3)
    watch = asyncio.create_task(runner.MachineRunner._watch_loop_lag(None))
    with caplog.at_level(logging.WARNING, logger=runner.logger.name):
        await asyncio.sleep(1.5)
        assert not [record for record in caplog.records if "answered nothing" in record.getMessage()]
        time.sleep(0.9)   # a synchronous call on the loop
        await asyncio.sleep(1.5)
    watch.cancel()
    said = [record.getMessage() for record in caplog.records if "answered nothing" in record.getMessage()]
    assert len(said) == 1 and "load average" in said[0]
