"""What a line shown off this PC never carries: credential shapes, and the exact secret values the PC knows."""

import re


#: Most characters of a private key's body a mask covers past its BEGIN line.
PEM_BODY = 32 * 1024
#: Credential shapes never shown on the web, whatever line carries them: each pattern's first group
#: (what names the secret) is kept, the rest masked.
_SECRET_SHAPES = (
    # A key's body masked up to its END line (a PEM or an OpenPGP key), or (none within PEM_BODY) its next PEM_BODY
    # characters: no real key is longer, and a mask reaching no further lets a page of a file be redacted from a bounded
    # window around it. A PuTTY key alike, from its first line to its MAC.
    re.compile(rf"(-----BEGIN [A-Z ]{{0,40}}PRIVATE KEY(?: BLOCK)?-----)(?:[\s\S]{{0,{PEM_BODY}}}?-----END [A-Z ]{{0,40}}PRIVATE KEY(?: BLOCK)?-----|[\s\S]{{0,{PEM_BODY}}})"),
    re.compile(rf"(PuTTY-User-Key-File-\d+:)(?:[\s\S]{{0,{PEM_BODY}}}?Private-MAC:[ \t]*[0-9A-Fa-f]+|[\s\S]{{0,{PEM_BODY}}})"),
    # Bounded on each side of the word: unbounded, a long run of name characters backtracks quadratically.
    re.compile(r"(?i)(\b[\w.-]{0,40}(?:api[_-]?key|secret|token|password|passwd|credential|private[_-]?key|key[_-]?data)[\w.-]{0,40}[\"']?\s*[:=]\s*[\"']?)"
               r"(?![\d.]+(?:[\s\"',;]|$))[^\s\"',;]{8,}"),
    re.compile(r"(?i)\b(AGE-SECRET-KEY-1)[0-9A-Z]{20,}"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"\b(iwm_|iwk_|sk-|sk_live_|rk_live_|ghp_|gho_|github_pat_|glpat-|xox[bpas]-)[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_\-]{30,}|\b(hf_)[A-Za-z0-9]{30,}|\b(AKIA)[0-9A-Z]{16}\b|\b(eyJ)[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"),
    re.compile(r"\b(\d{8,10}:)[A-Za-z0-9_-]{35}\b"),
    re.compile(r"(://[^/\s:@]{1,256}:)[^@\s]{1,512}(?=@)"),
)


def redact(text: str, secrets: tuple[str, ...] = (), start: int = 0, end: int | None = None) -> str:
    """`text[start:end]` with credential shapes and the exact `secrets` (this runner's own key values) masked, each
    found in the whole of `text`: a mask crossing either edge still covers the part inside."""
    end = len(text) if end is None else end
    shown, at = [], start
    for left, right in masks(text, secrets):
        if right <= start or left >= end:
            continue
        shown += [text[at:max(left, start)], "•••"]
        at = min(right, end)
    return "".join((*shown, text[at:end]))


def masks(text: str, secrets: tuple[str, ...] = ()) -> list[tuple[int, int]]:
    """The character spans of `text` masked, in order, overlapping ones merged: each credential shape past its first
    group (what names the secret), each exact secret."""
    spans = []
    for shape in _SECRET_SHAPES:
        for found in shape.finditer(text):
            named = next((index for index, group in enumerate(found.groups(), 1) if group), None)
            spans.append((found.start() if named is None else found.end(named), found.end()))
    for secret in filter(None, secrets):
        at = text.find(secret)
        while at >= 0:
            spans.append((at, at + len(secret)))
            at = text.find(secret, at + 1)
    merged: list[tuple[int, int]] = []
    for left, right in sorted(span for span in spans if span[0] < span[1]):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


#: Bytes of a file read as raw: every one but printable ASCII, tab and line ends read as a line end, so no shape runs
#: across a binary file's structure.
_PLAIN = bytes(byte if 0x20 <= byte < 0x7f or byte in b"\t\n\r" else 0x0a for byte in range(256))
#: What every credential shape holds (lower case; a bot token's `:` after its digits): a raw scan runs the shapes only
#: around them, SHAPE_REACH characters on (a whole megabyte through every shape takes a quarter second).
_TRIGGERS = ("-----begin ", "putty-user-key-file-", "age-secret-key-1", "private_key", "private-key", "privatekey", "key_data", "key-data", "keydata", "api_key", "api-key", "apikey", "secret", "token", "password", "passwd", "credential", "bearer", "iwm_", "iwk_", "sk-", "sk_live_",
             "rk_live_", "ghp_", "gho_", "github_pat_", "glpat-", "xoxb-", "xoxp-", "xoxa-", "xoxs-", "aiza", "hf_", "akia", "eyj", "://", ":")
SHAPE_REACH = 4096


def holds_secret(data: bytes, needles: tuple[bytes, ...]) -> bool:
    """Whether a file's raw `data` carries one of `needles` (secret values, as each encoding writes them) or a
    credential shape, written in ASCII / UTF-8 or in UTF-16 (either byte order: both alignments)."""
    if any(needle in data for needle in needles):
        return True
    views = (data.translate(_PLAIN).decode("ascii"),
             *(data[at:].decode("utf-16-le", errors="replace").encode("ascii", errors="replace").translate(_PLAIN).decode("ascii") for at in (0, 1)))
    return any(shape.search(view, left, right) is not None for view in views for left, right in _around(view) for shape in _SECRET_SHAPES)


def _around(view: str) -> list[tuple[int, int]]:
    """The spans of `view` a credential shape may lie in: around each trigger, merged."""
    lower, found = view.lower(), []
    for trigger in _TRIGGERS:
        at = lower.find(trigger)
        while at >= 0:
            if trigger != ":" or view[max(at - 8, 0):at].isdigit():
                found.append(at)
            at = lower.find(trigger, at + 1)
    spans: list[tuple[int, int]] = []
    for at in sorted(found):
        left, right = max(at - 64, 0), at + SHAPE_REACH
        if spans and left <= spans[-1][1]:
            spans[-1] = (spans[-1][0], right)
        else:
            spans.append((left, right))
    return spans


def mask(text: str, secrets: tuple[str, ...]) -> str:
    """`text` with each of the exact `secrets` masked, nothing else touched."""
    for secret in secrets:
        text = text.replace(secret, "•••")
    return text


def secret_values(environment: dict[str, str]) -> tuple[str, ...]:
    """The values in `environment` that are credentials (their names say so), longest first."""
    named = (value for key, value in environment.items() if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I) and len(value) >= 8)
    return tuple(sorted(set(named), key=len, reverse=True))
