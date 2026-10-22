"""What a coordinator's card quotes from the project's record of the person's own words when a turn carries his words
(`Config.agent_record_recall`): an ask he made before is in front of the coordinator the turn he raises it again, never
left to its remembering the record exists. Context only: the record says what he said, never what is done."""

from __future__ import annotations

import math
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from galaius.config.settings import RecordRecall

#: An entry's heading (`## 2026-10-16 — one deploy owner`) and its « SAID » lines.
HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2})\b[^\n]*$", re.M)
SAID = re.compile(r"^SAID\b[^:]*:\s*(.+)$", re.M)


class Entry(BaseModel):
    """One entry of the record: its date and what he said there."""

    model_config = ConfigDict(frozen=True)

    date: str
    said: str


def entries(record: Path) -> list[Entry]:
    """Each entry of `record` that carries his words, in the record's order."""
    text = record.read_text(encoding="utf-8", errors="replace")
    starts = [*HEADING.finditer(text)]
    found = []
    for index, heading in enumerate(starts):
        block = text[heading.end():starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        said = " ".join(line.group(1).strip() for line in SAID.finditer(block))
        if said:
            found.append(Entry(date=heading.group(1), said=said))
    return found


def recalled(record: Path, words: str, recall: RecordRecall) -> list[Entry]:
    """The entries of `record` that `words` speak of again (`RecordRecall`), best first."""
    if not recall.enabled or not words.strip() or not record.is_file():
        return []
    known = entries(record)
    lexicon = [_words(entry.said, recall.letters) for entry in known]
    # How rare each word is in the record, 1 for a word in one entry only: a score counts rare words whatever its size.
    scale = math.log((len(lexicon) + 1) / 2) or 1.0
    rarity = {word: math.log((len(lexicon) + 1) / (sum(word in said for said in lexicon) + 1)) / scale for word in set().union(*lexicon)}
    mine = _words(words, recall.letters)
    scored = []
    for index, said in enumerate(lexicon):
        shared = mine & said
        if len(shared) >= recall.shared and (score := sum(rarity[word] for word in shared)) >= recall.score:
            scored.append((score, index))
    return [known[index] for _score, index in sorted(scored, reverse=True)[:recall.shown]]


def _words(text: str, letters: int) -> frozenset[str]:
    """Its content words: `letters` letters or more, lowercased, a plural's `s` folded."""
    return frozenset(word.lower().removesuffix("s") if len(word) > letters else word.lower()
                     for word in re.findall(rf"[^\W\d_]{{{letters},}}", text))
