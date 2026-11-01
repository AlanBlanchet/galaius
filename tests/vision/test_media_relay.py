"""A galaius server inside an agent fence (`Config.media_relay`) asks its media analyses of the relay outside it
(`galaius.vision.relay`): the request reaches the outside's `analyze_media` intact, its result or failure comes back,
and what anything inside could forge (a remote schema reference, a model id routing a key elsewhere) is refused
before a model call. The outside's model call is a stand-in."""

import asyncio
import base64
import contextlib
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

import galaius.vision.relay.__main__ as outside
from galaius.config.settings import Config
from galaius.vision.core import UNSET, _native_response_format, analyze_media
from galaius.vision.relay import MediaRelay, MediaRequest
from galaius.vision.types import MediaItem, VLMResult

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()


class _Verdict(BaseModel):
    passed: bool


@contextlib.asynccontextmanager
async def _outside(socket: Path, answer, monkeypatch: pytest.MonkeyPatch):
    """The relay serving `socket`, its `analyze_media` replaced by `answer` (each call's arguments recorded)."""
    calls: list[dict] = []

    async def outside_analyze(media, context, config, prompt, max_tokens, response_format, **kwargs):
        calls.append({"media": media, "context": context, "prompt": prompt, "max_tokens": max_tokens,
                      "response_format": response_format, "media_relay": config.media_relay, **kwargs})
        kwargs["_dispatch_state"].append(True)
        return answer()

    monkeypatch.setattr(outside, "analyze_media", outside_analyze)
    server = asyncio.create_task(outside.serve(socket, Config(media_relay=None)))
    try:
        yield calls
    finally:
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server


def _inside(socket: Path, response_format=_Verdict, model: str = "cap.vlm"):
    return analyze_media([MediaItem(data=_PNG)], "page", Config(media_relay=socket), "is it ok?", 300, response_format,
                         model=model, role="component")


def test_a_fenced_analysis_runs_outside_with_its_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket = tmp_path / "media.sock"

    async def scenario():
        async with _outside(socket, lambda: VLMResult(text='{"passed": true}', elapsed=0.4, model="gemini/x", backend="api"), monkeypatch) as calls:
            dispatched: list[bool] = []
            result = await analyze_media([MediaItem(data=_PNG)], "page", Config(media_relay=socket), "is it ok?", 300, _Verdict,
                                         model="cap.vlm", role="component", _api_model="openai/elsewhere", _dispatch_state=dispatched)
            return result, calls, dispatched

    result, calls, dispatched = asyncio.run(scenario())
    assert (result.text, result.model, dispatched) == ('{"passed":true}', "gemini/x", [True])
    [call] = calls
    assert call["media"][0].data == _PNG and (call["context"], call["prompt"], call["model"], call["role"]) == ("page", "is it ok?", "cap.vlm", "component")
    assert call["response_format"] == _Verdict.model_json_schema() and call["media_relay"] is None  # the outside never relays again
    assert call["max_tokens"] is UNSET and "_api_model" not in call  # the outside's own cap and API model, never the asked ones


@pytest.mark.parametrize(("model", "schema", "refusal"), [
    ("ollama/llama3", None, "neither a criterion nor a model"),  # a key sent to a local port
    ("github/gpt-4o", None, "neither a criterion nor a model"),
    ("cap.vlm", {"type": "object", "properties": {"a": {"$ref": "https://attacker.example/s.json"}}}, "refers only inside itself"),
    ("cap.vlm", {"$id": "http://169.254.169.254/latest/", "type": "object"}, "no $id"),
    ("cap.vlm", {"type": "object", "$defs": {"x": {"$dynamicRef": "file:///etc/passwd"}}}, "no $dynamicRef"),
    # validated outside, synchronously, after the paid call: each costs exponential time in its depth
    ("cap.vlm", {"$defs": {"l0": {"type": "string"}, **{f"l{i}": {"allOf": [{"$ref": f"#/$defs/l{i - 1}"}] * 2} for i in range(1, 41)}},
                 "$ref": "#/$defs/l40"}, "unrolls to at most"),
    ("cap.vlm", {"$defs": {"l0": {"type": "string"}, **{f"l{i}": {"allOf": [{"$dynamicRef": f"#/$defs/l{i - 1}"}] * 2} for i in range(1, 41)}},
                 "$ref": "#/$defs/l40"}, "no $dynamicRef"),
    ("cap.vlm", {"$defs": {"l0": {"$anchor": "l0", "type": "string"}, "l1": {"allOf": [{"$ref": "#l0"}] * 2}}, "$ref": "#/$defs/l1"}, "no $anchor"),
    ("cap.vlm", {"$defs": {"n": {"type": "array", "items": {"allOf": [{"$ref": "#/$defs/n"}] * 2}}}, "$ref": "#/$defs/n"}, "not recursive"),
    ("cap.vlm", {"type": "string", "pattern": "^(a+)+$"}, "no pattern"),  # backtracking
])
def test_a_forged_request_is_refused_before_any_model_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model, schema, refusal) -> None:
    socket = tmp_path / "media.sock"

    async def scenario():
        async with _outside(socket, lambda: pytest.fail("a model was called"), monkeypatch) as calls:
            with pytest.raises(ValueError, match=re.escape(refusal)):  # refused inside, as an honest server asks
                await _inside(socket, schema, model)
            # ... and outside, as anything inside can write to the socket
            while not socket.exists():
                await asyncio.sleep(0.05)
            reader, writer = await asyncio.open_unix_connection(socket)
            forged = MediaRequest.model_construct(media=[MediaItem(data=_PNG)], context="page", prompt=None, response_format=schema,
                                                  model=model, role="image")
            writer.write(forged.model_dump_json().encode() + b"\n")
            reply = outside.MediaReply.model_validate_json(await reader.readline())
            writer.close()
            return reply, calls

    reply, calls = asyncio.run(scenario())
    assert reply.result is None and refusal in reply.error and reply.kind == "ValueError" and calls == []


_KEY = "sk-test-0123456789abcdef"


def _leak():
    raise TimeoutError(f"provider timed out on https://generativelanguage.googleapis.com/v1/models?key={_KEY}")


@pytest.mark.parametrize(("answer", "response_format"), [
    (_leak, _Verdict),  # a failure's text
    (lambda: VLMResult(text=f"[Fallback: used gemini/x after openai/y failed: 401 key {_KEY} invalid]\nok", elapsed=1.0), None),  # a success's
])
def test_no_key_of_the_outside_reaches_the_inside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer, response_format) -> None:
    socket = tmp_path / "media.sock"
    monkeypatch.setenv("GEMINI_API_KEY", _KEY)

    async def scenario():
        async with _outside(socket, answer, monkeypatch):
            return await _inside(socket, response_format)

    try:
        seen = asyncio.run(scenario()).text
    except TimeoutError as raised:  # its kind kept
        seen = str(raised)
    assert _KEY not in seen and "[redacted]" in seen


def test_an_absent_relay_is_named_not_waited_on_forever(tmp_path: Path) -> None:
    request = MediaRequest.of([MediaItem(data=_PNG)], "page", "q", None, model="", role="image")
    with pytest.raises(RuntimeError, match="not answering"):
        asyncio.run(MediaRelay(socket=tmp_path / "none.sock", ready_seconds=0.2).analyze(request, None, None, deadline=1))


def test_parallel_analyses_wait_their_turn_never_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent firing more vision calls at once than the outside serves gets every one answered."""
    socket = tmp_path / "media.sock"

    async def scenario():
        async with _outside(socket, lambda: VLMResult(text='{"passed": true}', elapsed=0.1), monkeypatch) as calls:
            results = await asyncio.gather(*(_inside(socket) for _ in range(outside.OPEN + 3)))
            return results, calls

    results, calls = asyncio.run(scenario())
    assert [result.text for result in results] == ['{"passed":true}'] * (outside.OPEN + 3) and len(calls) == outside.OPEN + 3


def test_the_schemas_of_galaius_own_tools_are_relayed() -> None:
    from galaius.server.tools.vision import UIReview, VerifyReport
    from galaius.vision.detect import _DetectionResult
    for model in (UIReview, VerifyReport, _DetectionResult):
        MediaRequest.of([MediaItem(data=_PNG)], "page", "q", model, model="", role="image")


def test_a_relay_dying_mid_request_is_named(tmp_path: Path) -> None:
    socket = tmp_path / "media.sock"

    async def scenario():
        async def die(reader, writer):
            await reader.readline()
            writer.close()

        async with await asyncio.start_unix_server(die, socket, limit=2**24):
            await _inside(socket)

    with pytest.raises(RuntimeError, match="closed the connection without an answer"):
        asyncio.run(scenario())


_SCHEMA = _Verdict.model_json_schema()


@pytest.mark.parametrize(("given", "sent"), [
    (_Verdict, _Verdict),
    (_SCHEMA, {"type": "json_schema", "json_schema": {"name": "_Verdict", "schema": _SCHEMA}}),
    ({"type": "object", "title": "a verdict"}, {"type": "json_schema", "json_schema": {"name": "a_verdict", "schema": {"type": "object", "title": "a verdict"}}}),
])
def test_the_schema_reaches_the_model_as_a_response_format(given, sent) -> None:
    """The relay carries a schema as JSON: handed to litellm bare, Gemini answered it with nothing parseable."""
    assert _native_response_format(given) == sent
