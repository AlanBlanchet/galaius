"""Media made by the Galaius platform for this computer's person (`galaius_core.media`): every capability's tool asks the
same way — a typed spec, a quote first, then the job within the cost the caller accepts — and every file a job makes is
saved and kept like a file `create_file` writes (`files.save`), so it shows in the person's chat as a file to download.
No provider key on this computer is used: each job is a run charged to him (his ceilings and the company's apply),
listed in the month's Facturation, linked to the agent run that asked."""

import base64
import hashlib
from pathlib import PurePosixPath

from galaius_core import MEDIA_INPUT_BYTES, MediaAnswer, MediaFile, MediaRequest, MediaSpec, file_token
from pydantic import BaseModel, ConfigDict

from galaius.files import Written, read_working, save, writable
from galaius.images import Images
from galaius.server_workspace import ServerWorkspace

#: Where a job's files go, under the folder the caller works in.
FOLDER = "galaius-media"


class MediaMade(BaseModel):
    """What one ask left: the platform's answer and each file saved here."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    answer: MediaAnswer
    written: tuple[Written, ...] = ()

    def said(self) -> str:
        """The answer as an agent or a terminal reads it: the quote, or the files made, what they cost and the run."""
        quote = self.answer.quote
        price = f"${quote.low_usd:.4f}" if quote.low_usd == quote.high_usd else f"${quote.low_usd:.4f} to ${quote.high_usd:.4f}"
        if self.answer.run_id is None:
            return (f"Quote: {price} on {quote.provider}/{quote.model}, platform fee included. Nothing made, nothing charged: "
                    f"ask again with max_cost_usd of at least {quote.high_usd:.4f} to make it.")
        cost = "price unknown" if self.answer.cost_usd is None else f"${self.answer.cost_usd:.4f}"
        lines = [f"Made by {quote.provider}/{quote.model}, {cost} charged to the person you work for in Galaius (Facturation), run {self.answer.run_id}:"]
        lines += [f"  {written.path} ({written.size:,} bytes)" + ("" if written.key is None else f" {file_token(written.key, written.path.name)}")
                  for written in self.written]
        if any(written.key is not None for written in self.written):
            lines.append("Each file shows in the person's chat as a file to download.")
        if self.answer.text is not None:
            lines += ["", self.answer.text]
        return "\n".join(lines)


class Media:
    @staticmethod
    def ask(spec: MediaSpec, *, max_cost_usd: float | None, folder: str, idempotency_key: str | None = None) -> MediaMade:
        """`spec` asked of the platform: a quote when `max_cost_usd` is None, else made within it, each file it made
        saved under `folder` in the working folder (`files.save`); `idempotency_key`: the same key again answers the
        same job, paid once."""
        request = MediaRequest(spec=spec, max_cost_usd=max_cost_usd, caller_run=Images.caller())
        if max_cost_usd is not None:
            writable(str(PurePosixPath(folder) / "probe.txt"))  # a job that could not be kept is never paid for
        # The same ask again (a retried call) is the same job, paid once: its key is what it asks.
        key = idempotency_key or hashlib.sha256(f"{Images.caller()}:{request.model_dump_json()}".encode()).hexdigest()
        answer = ServerWorkspace.configured().make_media(request, key)
        return MediaMade(answer=answer, written=tuple(Media._saved(folder, made) for made in answer.files))

    @staticmethod
    def _saved(folder: str, made: MediaFile) -> Written:
        """`made` saved under `folder` (`files.save`), its name's last part only (it comes from the platform). A file of
        that name already there is never written over, unless it is the same bytes (the same job answered again)."""
        path, content = str(PurePosixPath(folder) / PurePosixPath(made.name).name), base64.b64decode(made.data_base64, validate=True)
        try:  # read as any working file is (no link, at most this file's size): only the very same bytes are written over
            same = read_working(path, len(content)) == content
        except (ValueError, OSError):  # PermissionError is an OSError
            same = False
        return save(path, content, replace=same)

    @staticmethod
    def read_input(path: str) -> bytes:
        """A file the caller names as a job's input (`files.read_working`: inside its working folder, where its run may
        read, at most `MEDIA_INPUT_BYTES`)."""
        return read_working(path, MEDIA_INPUT_BYTES)
