"""What a line shown off this PC never carries: credential shapes, and the exact secret values the PC knows."""

import re


#: Credential shapes never shown on the web, whatever line carries them: each pattern's first group
#: (what names the secret) is kept, the rest masked.
_SECRET_SHAPES = (
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
    # Bounded on each side of the word: unbounded, a long run of name characters backtracks quadratically.
    re.compile(r"(?i)(\b[\w.-]{0,40}(?:api[_-]?key|secret|token|password|passwd|credential)[\w.-]{0,40}[\"']?\s*[:=]\s*[\"']?)(?![\d.]+(?:[\s\"',;]|$))[^\s\"',;]{8,}"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"\b(iwm_|iwk_|sk-|sk_live_|rk_live_|ghp_|gho_|github_pat_|glpat-|xox[bpas]-)[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_\-]{30,}|\b(hf_)[A-Za-z0-9]{30,}|\b(AKIA)[0-9A-Z]{16}\b|\b(eyJ)[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"),
    re.compile(r"\b(\d{8,10}:)[A-Za-z0-9_-]{35}\b"),
    re.compile(r"(://[^/\s:@]{1,256}:)[^@\s]{1,512}(?=@)"),
)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """`text` with credential shapes and the exact `secrets` (this runner's own key values) masked."""
    for shape in _SECRET_SHAPES:
        text = shape.sub(lambda found: next((group for group in found.groups() if group), "") + "•••", text)
    return mask(text, secrets)


def mask(text: str, secrets: tuple[str, ...]) -> str:
    """`text` with each of the exact `secrets` masked, nothing else touched."""
    for secret in secrets:
        text = text.replace(secret, "•••")
    return text


def secret_values(environment: dict[str, str]) -> tuple[str, ...]:
    """The values in `environment` that are credentials (their names say so), longest first."""
    named = (value for key, value in environment.items() if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I) and len(value) >= 8)
    return tuple(sorted(set(named), key=len, reverse=True))
