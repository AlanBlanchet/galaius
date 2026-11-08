"""Token counts in what an older runtime wrote on this PC — its run records, their events, the media usage log — read
once, at each model's own validation, into `galaius_core.TokenCounts`: the shape every reader, the panel and the server
see. An older record read as « nothing counted » would undercount; a cached token kept inside a whole-prompt figure would
be counted twice."""

from galaius_core import TokenCounts

#: An older runtime's spelling of the tokens on an event or a run record: the WHOLE prompt as `input_tokens`, its cache
#: reads and writes inside it (`TokenCounts.of_prompt`'s arguments, in order).
OLDER_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens")


def older_tokens(written: object) -> object:
    """An event, run record or usage row an older runtime wrote, its whole-prompt token fields as its `usage`; a row
    already holding `usage` keeps it."""
    if not isinstance(written, dict) or not written.keys() & set(OLDER_TOKEN_FIELDS):
        return written
    value = {key: item for key, item in written.items() if key not in OLDER_TOKEN_FIELDS}
    figures = [written.get(key) for key in OLDER_TOKEN_FIELDS]
    if not isinstance(written.get("usage"), dict | TokenCounts) and (figures[0] is not None or figures[1] is not None):
        value["usage"] = TokenCounts.of_prompt(*figures).model_dump(mode="json", exclude_none=True)
    return value
