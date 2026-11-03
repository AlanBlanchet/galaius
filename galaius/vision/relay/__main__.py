"""The media relay's outside half: `python -I -m galaius.vision.relay <socket>`, started by a fence's `outer` and
killed with it. It answers each connection's one `MediaRequest` with this PC's settings and keys, after checking it:
anything inside the fence can write to the socket."""

import asyncio
import sys
from pathlib import Path

from galaius.catalog.criteria import Criteria
from galaius.config.settings import Config
from galaius.config.user import UserConfig
from galaius.isolation.fence import without_secrets
from galaius.models.registry import Model
from galaius.vision.core import MAX_API_FALLBACKS, UNSET, analyze_media
from galaius.vision.relay import FRAME, OPEN, MediaReply, MediaRequest
from galaius.vision.session import redacted

#: Connections served at once, their request read included (`OPEN` open at once, the rest answered "busy": each one
#: buffers up to twice the stream limit); how long one may take to send its request.
PARALLEL, READ_SECONDS = 2, 30.0


def _model(asked: str) -> str:
    """`asked` as `analyze_media` takes it outside: empty, a criterion, or a model of this PC's catalog it has a key
    for. Any other id is refused: the provider prefix alone picks the host a key is sent to (`ollama/` sends it to
    a local port, `github/` to another vendor)."""
    if not asked or Criteria.written(asked):
        return asked
    if (row := Model.by_id(asked)) is None or not row.is_available():
        raise ValueError(f"model {asked!r} is neither a criterion nor a model this PC's catalog has a key for and its provider still serves")
    return asked


async def answer(request: MediaRequest, config: Config) -> MediaReply:
    """`request` analysed here; its failure, if any, as a reply (never raised: the inside always hears back)."""
    dispatched: list[bool] = []
    try:
        result = await asyncio.wait_for(
            analyze_media(request.media, request.context, config, request.prompt, UNSET, request.response_format,
                          model=_model(request.model), role=request.role, _dispatch_state=dispatched),
            config.media_timeout * (MAX_API_FALLBACKS + 1))
        return MediaReply(result=result, dispatched=bool(dispatched))
    except Exception as exc:
        return MediaReply.failed(exc, redacted(f"{type(exc).__name__}: {exc}"), bool(dispatched))


async def serve(socket: Path, config: Config) -> None:
    turns, held = asyncio.Semaphore(PARALLEL), 0

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal held
        held += 1
        try:
            if held > OPEN:
                reply = MediaReply(error=f"the media relay is busy: {OPEN} connections open", kind="RuntimeError")
                writer.write(reply.model_dump_json().encode() + b"\n")
                await writer.drain()
                return
            async with turns:
                try:
                    line = await asyncio.wait_for(reader.readline(), READ_SECONDS)
                    request = MediaRequest.model_validate_json(line)
                except Exception as exc:  # unreadable, oversized, slow or malformed: refused, answered
                    reply = MediaReply(error=redacted(f"the media relay refused the request: {type(exc).__name__}: {exc}"), kind="ValueError")
                else:
                    reply = await answer(request, config)
                # every field, a success's too: a fallback's note quotes the failed model's error
                writer.write(without_secrets(reply.model_dump_json()).encode() + b"\n")
                await writer.drain()
        finally:
            held -= 1
            writer.close()

    server = await asyncio.start_unix_server(connection, socket, limit=config.media_max_total_bytes * 4 // 3 + FRAME)
    async with server:
        await server.serve_forever()


def main(socket: Path) -> None:
    UserConfig.apply(portable=False)
    asyncio.run(serve(socket, Config(media_relay=None)))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
