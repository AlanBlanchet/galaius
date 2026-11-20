"""create_file: a file in any format the person opens, written here by galaius at no cost; keep_file and open_file: any file
an agent made any way, kept under its key and written back from it."""

import asyncio

from galaius_core import FILE_REFUSED, file_token
from pydantic import ValidationError

from galaius.agents.catalog.community import quote
from galaius.media.files import Cell, FileSpec, keep, open_kept, write
from galaius.server.core import instrumented, mcp


@mcp.tool(category="media")
@instrumented
async def create_file(
    path: str,
    content: str = "",
    content_from: str | None = None,
    rows: list[list[Cell]] | None = None,
    sheets: dict[str, list[list[Cell]]] | None = None,
    subject: str = "",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    attach: list[str] | None = None,
    replace: bool = False,
    from_files: list[str] | None = None,
    replaces: str | None = None,
) -> str:
    """Write a file the person opens — a document, a spreadsheet, a deck, a PDF, a mail draft — in your working folder.

    Use this for every file meant for the person: it is written here at no cost, and shows in their chat as a file
    they can download. `path` is relative to your working folder (folders made when missing); its suffix picks the
    format and what it reads:

    - .txt, .md: `content` as is. .json: `content`, which must be valid JSON.
    - .csv: `rows` (the first row is the header).
    - .xlsx: `rows`, or `sheets` {sheet name: rows} for several (numbers stay numbers; header in bold).
    - .docx: `content` as Markdown-lite (`#` headings, `-` bullets, paragraphs), then `rows` as a table.
    - .pptx: `content` as Markdown-lite, one slide per `#` heading with its bullets.
    - .pdf: `content` as Typst markup (`= Title`, `- item`, `*bold*`), then `rows` as a table.
    - .eml: a mail DRAFT the person sends themselves: `to`, `cc`, `subject`, `content` as its body, `attach` the paths
      of files you wrote with create_file in this run (a report, a sheet; 4 MiB together). Never sent from here.

    content_from: instead of `content`, the absolute path of a file a community agent you started wrote in its own
    folder (a deck's Markdown-lite, a page's Typst): its text becomes `content` without you reading it, so a stranger's
    words reach the person, never your context. Only that agent's folder; check its report before passing it on.

    from_files, replaces: the keys of the kept files you made this from (a sheet it summarises, a draft it improves) and the
    one it takes the place of; keys you made or were handed, else refused. They show in the person's chat and tell
    the agent that reads it what it came from.

    replace: write over a file already at `path` (refused otherwise). Written only where you may write on this
    computer, never as an agent's steering file (CLAUDE.md, AGENTS.md…). Returns where it was written and its size, or
    why nothing was written.
    """
    try:
        spec = FileSpec(path=path, content=content, content_from=content_from, rows=rows or [], sheets=sheets or {}, subject=subject, to=to or [], cc=cc or [],
                        attach=attach or [])
        written = await asyncio.to_thread(lambda: write(spec, replace=replace, sources=from_files or (), replaces=replaces))
    except ValidationError as error:
        return FILE_REFUSED + "; ".join(str(problem["msg"]).removeprefix("Value error, ") for problem in error.errors())
    except (ValueError, OSError) as error:
        # A refusal of a stranger's text (a Typst error cites its lines) reaches the caller quoted, as its report does.
        return f"{FILE_REFUSED}{quote(str(error), 2000) if content_from is not None else error}"
    said = f"Wrote {written.path} ({spec.suffix[1:]}, {written.size:,} bytes)."
    # The token is how the chat finds the file to offer (the copy kept under that key), never a path.
    return said if written.key is None else f"{said} It shows in the person's chat as a file to download. {file_token(written.key, written.path.name, written.story)}"


@mcp.tool(category="media")
@instrumented
async def keep_file(path: str, from_files: list[str] | None = None, replaces: str | None = None) -> str:
    """Keep a file you made any way you like — a shell, a program on this PC, another tool — under its key, so the person sees it
    in their chat and other agents are handed it by that key.

    path: the file, relative to your working folder; any format; never a hidden file, a credential store, a file whose name says secret, token, password or key, nor one holding a private key. Its key is the
    hash of its bytes: the same bytes are the same key on every PC, and a copy of it is kept for your run, whatever you do
    to the file after. from_files: the keys of the kept files you made it from; replaces: the key of the one it takes
    the place of — both said by you (keys you made or were handed), never read from a name. Returns its key.
    """
    try:
        kept = await asyncio.to_thread(lambda: keep(path, sources=from_files or (), replaces=replaces))
    except (ValueError, PermissionError, OSError) as error:
        return f"{FILE_REFUSED}{error}"
    assert kept.key is not None
    return f"Kept {path} ({kept.size:,} bytes) as {kept.key}. It shows in the person's chat as a file to download. {file_token(kept.key, kept.path.name, kept.story)}"


@mcp.tool(category="media")
@instrumented
async def open_file(key: str, path: str, replace: bool = False) -> str:
    """Write the kept file `key` — one you made, or one another agent or the person handed you — into your working folder at
    `path` (relative; folders made when missing; any format), its bytes exactly the key's. replace: write over a file already
    there. Returns where it was written, or why not."""
    try:
        written = await asyncio.to_thread(lambda: open_kept(key, path, replace=replace))
    except (ValueError, PermissionError, OSError) as error:
        return f"Nothing written: {error}"
    return f"Wrote {written.path} ({written.size:,} bytes): the file kept as {key}."
