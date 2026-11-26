"""The key of a start: the same words keep it while the start lives (a retry never places two), a start the server ended frees it."""
from pathlib import Path
from uuid import uuid4

import pytest

from galaius_core import PeerRefusal
from galaius.machine.peer.client import PeerClient, PeerError


def keys_of_two_asks(tmp_path: Path, code: str) -> list[str]:
    client, sent = PeerClient(tmp_path / "peer.sock"), []

    def ask(call, seconds=0):
        sent.append(call.key)
        raise PeerError(PeerRefusal(code=code, message=f"refused: {code}"))

    client.ask = ask
    target, project = uuid4(), uuid4()
    for _ in range(2):
        with pytest.raises(PeerError):
            client.start(target, project, "tester", "run the tests")
    return sent


def test_a_start_that_needs_its_owner_is_over_so_the_next_ask_is_a_new_request(tmp_path: Path) -> None:
    first, second = keys_of_two_asks(tmp_path, "needs_owner")
    assert first != second


@pytest.mark.parametrize("code", ["pc_slow", "in_progress", "link_ceiling"])
def test_a_start_that_may_still_run_keeps_its_key(tmp_path: Path, code: str) -> None:
    first, second = keys_of_two_asks(tmp_path, code)
    assert first == second
