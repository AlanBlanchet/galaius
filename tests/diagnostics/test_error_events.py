"""A computer's errors reach its server as counted typed facts: one entry for a repeating error, no formatted text, no
foreign frames, a retried delivery keeps its batch id, `off` and a refused token silence it, its own failures never feed it."""

import logging

import httpx
import pytest

from galaius.diagnostics import error_events
from galaius.diagnostics.error_events import ErrorEvents


class Config:
    def endpoint(self, path: str, *, socket: bool = False) -> str:
        return f"http://server.test{path}"

    @property
    def authorization(self) -> dict[str, str]:
        return {"authorization": "Bearer x"}


class Server:
    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers, self.bodies = list(answers), []

    def post(self, url, json, headers, timeout):
        self.bodies.append(json)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(stored: int = 1, tracking: str = "on") -> httpx.Response:
    return httpx.Response(200, json={"stored": stored, "dropped": {}, "tracking": tracking})


@pytest.fixture
def handler() -> ErrorEvents:
    return ErrorEvents()


def log(handler: ErrorEvents, name: str, message: str, *args, exc: BaseException | None = None) -> None:
    logger = logging.Logger(name)
    logger.addHandler(handler)
    logger.error(message, *args, exc_info=exc)


def raised(error: BaseException) -> BaseException:
    try:
        raise error
    except BaseException as caught:  # noqa: BLE001
        return caught


def test_a_repeating_error_is_one_counted_entry_with_its_template_and_own_frames(handler, monkeypatch) -> None:
    server = Server(ok())
    monkeypatch.setattr(httpx, "post", server.post)
    for _ in range(4000):
        log(handler, "galaius.agents.run", "provider usage has no context_length", exc=raised(AttributeError("'Usage' object has no attribute 'context_length'")))
    assert handler.deliver(Config(), now=1000.0) == 1
    (batch,) = server.bodies
    (event,) = batch["events"]
    assert (event["name"], event["count"], event["source"], event["kind"]) == ("AttributeError", 4000, "pc", "pc_exception")
    assert event["message"] == "'Usage' object has no attribute 'context_length'"
    assert event["frames"] == [], "a frame of test code is not galaius's own"


def test_a_message_is_the_template_never_the_formatted_text_and_data_classes_send_none(handler, monkeypatch) -> None:
    server = Server(ok(2))
    monkeypatch.setattr(httpx, "post", server.post)
    log(handler, "galaius.x", "could not send %s to %s", "ma consigne privée", "ann@example.org")
    log(handler, "galaius.y", "failed", exc=raised(KeyError("jean.dupont")))
    handler.deliver(Config(), now=1000.0)
    sent = {event["name"]: event for event in server.bodies[0]["events"]}
    assert sent["LogTemplate"]["message"] == "could not send %s to %s"
    assert sent["KeyError"]["message"] is None
    assert "consigne" not in str(server.bodies) and "jean.dupont" not in str(server.bodies)


def test_a_failed_delivery_is_retried_as_the_same_batch_then_dropped(handler, monkeypatch) -> None:
    server = Server(httpx.ConnectError("down"), httpx.Response(503), ok())
    monkeypatch.setattr(httpx, "post", server.post)
    log(handler, "galaius.x", "boom")
    clock = 1000.0
    assert handler.deliver(Config(), now=clock) == 0
    assert handler.deliver(Config(), now=clock + 1) == 0 and len(server.bodies) == 1, "waits 2 s before the second try"
    assert handler.deliver(Config(), now=clock + 3) == 0
    assert handler.deliver(Config(), now=clock + 12) == 1
    assert len({body["batch_id"] for body in server.bodies}) == 1, "the server never counts a retried delivery twice"


def test_off_and_a_refused_token_silence_it_for_an_hour(handler, monkeypatch) -> None:
    server = Server(ok(0, "off"), httpx.Response(401))
    monkeypatch.setattr(httpx, "post", server.post)
    log(handler, "galaius.x", "one")
    handler.deliver(Config(), now=1000.0)
    log(handler, "galaius.x", "two")
    assert handler.deliver(Config(), now=1100.0) == 0 and len(server.bodies) == 1, "silent while the hour lasts"
    assert handler.deliver(Config(), now=1000.0 + error_events.SILENCE_SECONDS + 1) == 0 and len(server.bodies) == 2
    log(handler, "galaius.x", "three")
    assert handler.deliver(Config(), now=1000.0 + 2 * error_events.SILENCE_SECONDS) == 0 and len(server.bodies) == 2, "a refused token silences it"


def test_its_own_failures_and_the_http_client_never_feed_it_and_the_table_is_bounded(handler) -> None:
    log(handler, "galaius.diagnostics.error_events", "delivery failed")
    log(handler, "httpx", "HTTP Request failed")
    assert handler._seen == {}
    for number in range(error_events.MAX_KEYS + 5):
        log(handler, "galaius.x", f"template {number}")
    assert len(handler._seen) == error_events.MAX_KEYS and handler.dropped == 5
