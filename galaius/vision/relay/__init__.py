"""The media relay: a galaius server inside an agent fence has no model key and no route to a model host, so it
asks its media analyses of a process outside the fence, through a UNIX socket in the fence's egress folder.

Inside (`Config.media_relay`): `analyze_media` sends a `MediaRequest` and gets a `MediaReply`, one JSON line each
way, no key ever crossing. Outside (`galaius.vision.relay.__main__`, started by the fence's `outer` beside its egress
proxy): this PC's own settings and keys, every request checked there (anything inside can write to the socket).

Residual, stated: whoever is inside the fence spends this PC's media budget through it, on models its catalog has a
key for, as an unfenced galaius server's caller does."""

from pathlib import Path
from typing import Any, ClassVar, Literal, Self

import asyncio
import time

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from galaius.models.spec import ModelRole
from galaius.vision.types import MediaItem, VLMResult, compile_response_schema

#: Headroom over the media bytes (base64 4/3) for the context, prompt and schema of one request.
FRAME = 4 * 1024 * 1024
#: What a relayed schema is held to before it is taken: validating an answer against it runs outside, synchronously,
#: after the paid call (no deadline stops it). So: references only inside it (`#/...`, else validation fetches whatever
#: the PC reaches), none recursive and at most `EXPANSION` nodes unrolled (`allOf` of two references per level costs
#: 2^levels), no regular expression (backtracking). Every schema galaius's own tools send stays inside these.
EXPANSION = 10_000
REFUSED = ("$dynamicRef", "$recursiveRef", "$anchor", "$dynamicAnchor", "$id", "pattern", "patternProperties")
#: The keywords whose value holds subschemas: by name, as a list, as one.
_NAMED = ("properties", "$defs", "definitions", "dependentSchemas")
_LISTED = ("allOf", "anyOf", "oneOf", "prefixItems")
_SINGLE = ("items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames", "unevaluatedItems",
           "unevaluatedProperties")


def _pointed(root: Any, reference: str) -> Any:
    """The node `reference` (`#` or `#/a/b`, JSON pointer) names in `root`; None: none."""
    node = root
    for part in reference[2:].split("/") if reference != "#" else ():
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _unrolled(root: Any) -> int:
    """How many schema nodes `root` unrolls to, each `$ref` followed (each node counted once, memoised); raises
    ValueError on anything `REFUSED`, a reference outside it, a recursive one, or more than `EXPANSION` nodes."""
    sizes: dict[int, int] = {}
    walking: set[int] = set()

    def size(node: Any) -> int:
        if not isinstance(node, dict):
            return 1
        if id(node) in sizes:
            return sizes[id(node)]
        if id(node) in walking:
            raise ValueError("a relayed schema is not recursive: a reference back into itself refused")
        walking.add(id(node))
        total = 1
        for key, value in node.items():
            if key in REFUSED:
                raise ValueError(f"a relayed schema has no {key}: refused")
            if key == "$ref":
                if not (isinstance(value, str) and (value == "#" or value.startswith("#/"))) or (target := _pointed(root, value)) is None:
                    raise ValueError(f"a relayed schema refers only inside itself: $ref={str(value)[:80]!r} refused")
                total += size(target)
            elif key in _NAMED and isinstance(value, dict):
                total += sum(size(item) for item in value.values())
            elif key in _LISTED and isinstance(value, list):
                total += sum(size(item) for item in value)
            elif key in _SINGLE:
                total += size(value)
        walking.discard(id(node))
        if total > EXPANSION:
            raise ValueError(f"a relayed schema unrolls to at most {EXPANSION} nodes: this one to more")
        sizes[id(node)] = total
        return total

    try:
        return size(root)
    except RecursionError:
        raise ValueError("a relayed schema nests too deep: refused") from None


class MediaRequest(BaseModel):
    """`analyze_media`'s arguments on the wire, `response_format` as its JSON schema. Its token cap and API model
    are the outside's own settings', never asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    media: list[MediaItem]
    context: str
    prompt: str | None = None
    response_format: dict[str, Any] | None = None
    model: str = ""
    role: ModelRole

    @field_validator("response_format")
    @classmethod
    def _bounded(cls, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if schema is not None:
            _unrolled(schema)
        return schema

    @classmethod
    def of(cls, media: list[MediaItem], context: str, prompt: str | None,
           response_format: type[BaseModel] | dict[str, Any] | None, *, model: str, role: ModelRole) -> Self:
        return cls(media=media, context=context, prompt=prompt, response_format=compile_response_schema(response_format),
                   model=model, role=role)


#: The failures raised inside as they were outside; any other (a provider's `VisionError`) as RuntimeError.
Kind = Literal["RuntimeError", "ValueError", "TypeError", "TimeoutError"]


class MediaReply(BaseModel):
    """The outside's answer: its result or the failure it raised (its kind and its text); `dispatched` whether a
    backend was called (`analyze_media`'s `_dispatch_state`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    KINDS: ClassVar[dict[str, type[Exception]]] = {"RuntimeError": RuntimeError, "ValueError": ValueError, "TypeError": TypeError,
                                                   "TimeoutError": TimeoutError}

    result: VLMResult | None = None
    error: str = ""
    kind: Kind = "RuntimeError"
    dispatched: bool = False

    @model_validator(mode="after")
    def _result_or_error(self) -> Self:
        if (self.result is None) == (not self.error):
            raise ValueError("a media reply holds a result or an error, one of them")
        return self

    @classmethod
    def failed(cls, error: Exception, text: str, dispatched: bool = False) -> Self:
        """`error`, told as `text`, under the first kind it is (a `TimeoutError` before the `RuntimeError` it is not)."""
        kind = next((name for name, kind in cls.KINDS.items() if name != "RuntimeError" and isinstance(error, kind)), "RuntimeError")
        return cls(error=text, kind=kind, dispatched=dispatched)

    def raised(self) -> Exception:
        return self.KINDS[self.kind](self.error)


#: Requests one process inside sends at once per relay (per event loop), the rest waiting their turn: the outside
#: serves `OPEN` connections at once and answers any more "busy".
OPEN = 4
_TURNS: dict[tuple[Path, int], asyncio.Semaphore] = {}


class MediaRelay(BaseModel):
    """The relay at `socket`, asked from inside a fence. The outside owns the analysis deadline and always answers
    (or dies with the fence, closing the connection); the caller's `deadline` only bounds a relay stuck past it."""

    model_config = ConfigDict(frozen=True)
    socket: Path
    #: How long a caller waits for the socket to exist (the relay starts beside the fence, in parallel).
    ready_seconds: float = 10.0

    async def analyze(self, request: MediaRequest, response_format: type[BaseModel] | dict[str, Any] | None,
                      dispatch_state: list[bool] | None, deadline: float) -> VLMResult:
        """`request` analysed outside, its answer awaited at most `deadline` seconds once sent; the result held to
        `response_format` here too (`VLMResult.validated`)."""
        turns = _TURNS.setdefault((self.socket, id(asyncio.get_running_loop())), asyncio.Semaphore(OPEN))
        async with turns:
            line = await self._asked(request, deadline)
        reply = MediaReply.model_validate_json(line)
        if reply.dispatched and dispatch_state is not None:
            dispatch_state.append(True)
        if reply.result is None:
            raise reply.raised()
        return reply.result.validated(response_format)

    async def _asked(self, request: MediaRequest, deadline: float) -> bytes:
        ready = time.monotonic() + self.ready_seconds
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.socket, limit=FRAME)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= ready:
                    raise RuntimeError(f"the media relay at {self.socket} is not answering") from None
                await asyncio.sleep(0.1)
        try:
            try:
                writer.write(request.model_dump_json().encode() + b"\n")
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:  # answered before reading it all (too large, busy)
                cut = exc
            else:
                cut = None
            try:
                line = await asyncio.wait_for(reader.readline(), deadline)
            except TimeoutError:
                raise TimeoutError(f"the media relay at {self.socket} gave no answer in {deadline:.0f} s") from None
            except (ConnectionResetError, BrokenPipeError):
                line = b""
        finally:
            writer.close()
        if not line:
            raise RuntimeError(f"the media relay at {self.socket} closed the connection without an answer"
                               + (f" (the request was cut: {cut})" if cut else ""))
        return line
