"""create_file: a file in any format the person opens, written here by galaius at no cost."""

import asyncio

from galaius_core import FILE_REFUSED, file_token
from pydantic import ValidationError

from galaius.files import Cell, FileSpec, write
from galaius.server.core import instrumented, mcp


@mcp.tool(category="media")
@instrumented
async def create_file(
    path: str,
    content: str = "",
    rows: list[list[Cell]] | None = None,
    sheets: dict[str, list[list[Cell]]] | None = None,
    subject: str = "",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    attach: list[str] | None = None,
    replace: bool = False,
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

    replace: write over a file already at `path` (refused otherwise). Written only where you may write on this
    computer, never as an agent's steering file (CLAUDE.md, AGENTS.md…). Returns where it was written and its size, or
    why nothing was written.
    """
    try:
        spec = FileSpec(path=path, content=content, rows=rows or [], sheets=sheets or {}, subject=subject, to=to or [], cc=cc or [],
                        attach=attach or [])
        written = await asyncio.to_thread(write, spec, replace=replace)
    except ValidationError as error:
        return FILE_REFUSED + "; ".join(str(problem["msg"]).removeprefix("Value error, ") for problem in error.errors())
    except (ValueError, OSError) as error:
        return f"{FILE_REFUSED}{error}"
    said = f"Wrote {written.path} ({spec.suffix[1:]}, {written.size:,} bytes)."
    # The token is how the chat finds the file to offer (the copy kept under that key), never a path.
    return said if written.key is None else f"{said} It shows in the person's chat as a file to download. {file_token(written.key, written.path.name)}"
