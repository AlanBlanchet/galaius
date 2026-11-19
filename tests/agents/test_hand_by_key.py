"""Agents hand kept files by key: the recipient holds the bytes as handed, its message names them, both transcripts record them."""

import os
import uuid

import pytest
from galaius_core import file_key

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.media.files import FileSpec, write
from tests.support import register_run

MAKER, READER = str(uuid.UUID(int=1 << 96)), str(uuid.UUID(int=2 << 96))


@pytest.fixture
def pair(monkeypatch, tmp_path):
    monkeypatch.setattr(messaging, "policy_for_continuation", lambda run, provider, environment: (None, None, run.model, None))
    monkeypatch.setattr(messaging, "_dispatch", lambda *arguments, **keywords: None, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GALAIUS_RUN_ID", MAKER)
    register_run(MAKER, agent="maker", name="maker", task="make", pid=os.getpid(), provider_session_id=MAKER)
    register_run(READER, agent="reader", name="reader", task="read", pid=os.getpid(), provider_session_id=READER)


def test_a_message_hands_the_files_it_names_and_only_those_the_sender_holds(pair) -> None:
    made = write(FileSpec(path="out/sheet.xlsx", sheets={"A": [["x", 1]]}), replace=False)
    delivery = messaging.queue_message(READER, "check it", sender=MAKER, turn=agent_queue.MessageTurn(files=(made.key,)))
    assert delivery.state == "queued", delivery.text
    (item,) = [item for item in agent_queue.items(READER) if item.state == "pending"]
    prompt = item.turn.prompt(reg.get_run(READER), item.message_id)
    assert made.key in prompt and "sheet.xlsx" in prompt and f"run {MAKER[:8]}" in prompt
    (recorded,) = [event for event in reg.read_messages(READER) if event.files]
    assert [(file.key, file.name, file.story.maker) for file in recorded.files] == [(made.key, "sheet.xlsx", MAKER)]
    assert reg.RunFiles.path(READER, made.key).read_bytes() == made.path.read_bytes()
    # A key the sender never held, a path, and a person's own words naming files: refused, nothing queued, nothing kept.
    stranger = file_key(b"not mine", ".txt")
    refused = messaging.queue_message(READER, "x", sender=MAKER, turn=agent_queue.MessageTurn(files=(stranger,)))
    assert refused.state == "error" and "not a file this run holds" in refused.text and not reg.RunFiles.holds(READER, stranger)
    assert not any(item.message_id == refused.queue_id for item in agent_queue.items(READER))
    by_person = messaging.queue_message(READER, "x", sender=reg.PERSON_SENDER, turn=agent_queue.MessageTurn(files=(made.key,)))
    assert by_person.state == "error" and "only an agent run" in by_person.text


def test_a_file_handed_to_a_run_on_another_pc_goes_by_its_key_and_story_only_from_a_run_that_holds_it(pair, monkeypatch) -> None:
    from types import SimpleNamespace

    from galaius.server.tools import agents as tools
    seen = {}

    class Client:
        def message(self, machine, run_id, text, key=None, files=()):
            seen.update(machine=machine, text=text, files=files)
            return SimpleNamespace(message_id=uuid.uuid4())

    pc = SimpleNamespace(id=uuid.uuid4(), name="Desk 2")
    monkeypatch.setattr(tools, "_peer", lambda machine: (Client(), pc))
    made = write(FileSpec(path="out/sheet.xlsx", sheets={"A": [["x", 1]]}), replace=False)
    said = tools._peer_send("Desk 2", READER, "look", (made.key,))
    assert "with 1 file(s)" in said and [(file.key, file.name, file.story.maker) for file in seen["files"]] == [(made.key, "sheet.xlsx", MAKER)]
    assert tools._peer_send("Desk 2", READER, "look", (file_key(b"not mine", ".txt"),)).startswith("ERROR: ") and "not a file this run holds" in tools._peer_send("Desk 2", READER, "x", (file_key(b"not mine", ".txt"),))
