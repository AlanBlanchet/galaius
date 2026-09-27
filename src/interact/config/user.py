"""Settings shared by local clients. Connected portable preferences belong to the server.

The chmod-600 config.env file retains machine settings, credentials and portable recovery
evidence. Standalone clients also use its portable values; connected clients never do.
"""

import os
import re
from pathlib import Path

from interact.config.schema import by_key

# An already-environment-shaped name: all-caps, digits/underscores, no dots or dashes. A friendly
# setting key is always dotted (group.field), so this only matches real env vars.
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")

# Shell metacharacters that make a raw `KEY=value` line break (or execute) when the file is
# SOURCED — `KEY=cap.vlm and aa.intelligence >= 40` once ran `and` as a command on every source.
# Quoting at write time keeps the value intact for both readers: pydantic/python-dotenv strip the
# quotes, bash keeps the value whole instead of executing it.
_NEEDS_QUOTE_RE = re.compile(r"[^\w@%+=:,./-]")


def _quote_if_needed(value: str) -> str:
    """Single-quote a value containing characters bash would act on."""
    if _NEEDS_QUOTE_RE.search(value):
        return "'" + value.replace("'", "'\\''") + "'"
    return value


def _unquote(value: str) -> str:
    """Strip ONE level of single quotes the writer may have added.

    The file is one store with two readers — python-dotenv/pydantic (which
    strip quotes themselves) and `source` (which needs them). Reading through
    this class must return the value as SET, never the quoted form, or a
    criterion written with spaces parses as `'cap.vlm…` and fails.
    """
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("'\\'", "'")
    return value


class UserConfig:
    """Effective server/local settings, with separate raw local reads and mutations."""

    PATH = Path.home() / ".interact" / "config.env"
    _process_interact_env: dict[str, str] | None = None

    @classmethod
    def process_interact_env(cls) -> dict[str, str]:
        """The host/launcher's INTERACT_* values before this class applies config.env."""
        if cls._process_interact_env is None:
            cls._process_interact_env = {
                name: value
                for name, value in os.environ.items()
                if name.startswith("INTERACT_")
            }
        return dict(cls._process_interact_env)

    @classmethod
    def normalize_key(cls, key: str) -> str:
        """Map a friendly key to its environment-variable name.

        ``image.model`` / ``image-model`` → ``INTERACT_IMAGE_MODEL``; a key already in env form —
        ``OPENAI_API_KEY``, ``AWS_ACCESS_KEY_ID``, ``AZURE_API_BASE``, any ``INTERACT_*`` — is kept
        verbatim. The env-shape guard runs on the RAW key: after ``.replace(".", "_")`` a friendly
        key like ``image.model`` becomes ``IMAGE_MODEL``, indistinguishable from a real env var,
        so a provider cred whose name doesn't end in ``_API_KEY`` (AWS/Azure/Vertex) must be
        recognised BEFORE that rewrite — else it's stored under a dead ``INTERACT_*`` alias no SDK
        reads, and the provider silently never authenticates.
        """
        if _ENV_NAME_RE.fullmatch(key):
            return key
        setting = by_key(key)
        if setting is not None:
            return setting.env
        env = key.replace(".", "_").replace("-", "_").upper()
        if env.startswith("INTERACT_") or env.endswith("_API_KEY"):
            return env
        return f"INTERACT_{env}"

    @classmethod
    def read_local(cls) -> dict[str, str]:
        if not cls.PATH.exists():
            return {}
        out: dict[str, str] = {}
        for line in cls.PATH.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            out[name.strip()] = _unquote(value.strip())
        return out

    @classmethod
    def server(cls):
        # Circular dependency: the adapter validates Config, whose package exports UserConfig.
        from interact.server_tool_settings import ServerToolSettings

        return ServerToolSettings.configured()

    @classmethod
    def read(cls) -> dict[str, str]:
        data = cls.read_local()
        server = cls.server()
        if server is not None:
            from interact.server_tool_settings import PORTABLE_ENV  # circular Config/UserConfig wiring

            data = {name: value for name, value in data.items() if name not in PORTABLE_ENV}
            data.update(server.read().env)
        return data

    @classmethod
    def update(cls, changes: dict[str, str | None], *, base=None):
        """One server CAS for portable edits; local edits never serialize effective settings."""
        from interact.server_tool_settings import PORTABLE_ENV, ToolSettingsConflict  # circular Config/UserConfig wiring

        changes = {cls.normalize_key(key): value for key, value in changes.items()}
        server = cls.server()
        if base is not None and server is None:
            raise ToolSettingsConflict("Server connection removed. Reload before saving; draft retained.")
        portable = {key: value for key, value in changes.items() if key in PORTABLE_ENV} if server else {}
        saved = server.update(portable, base=base or server.read(allow_stale=False)) if portable else base
        local = {key: value for key, value in changes.items() if key not in portable}
        if local:
            cls._change_local(local)
        return saved

    @classmethod
    def _change_local(cls, changes: dict[str, str | None]) -> None:
        """Retain unrelated recovery entries, comments, ordering and quoting byte for byte."""
        original = cls.PATH.read_bytes().decode("utf-8") if cls.PATH.exists() else ""
        lines = [line for line in original.splitlines(keepends=True)
                 if line.strip().partition("=")[0].strip() not in changes]
        body = "".join(lines)
        additions = "".join(f"{key}={_quote_if_needed(value)}\n" for key, value in changes.items() if value is not None)
        if additions:
            body += ("\n" if body and not body.endswith("\n") else "") + additions
        if body != original:
            cls.PATH.parent.mkdir(parents=True, exist_ok=True)
            cls.PATH.write_bytes(body.encode("utf-8"))
            cls.PATH.chmod(0o600)

    @classmethod
    def get(cls, key: str) -> str | None:
        from interact.server_tool_settings import PORTABLE_ENV  # circular Config/UserConfig wiring

        env = cls.normalize_key(key)
        return (cls.read() if env in PORTABLE_ENV else cls.read_local()).get(env)

    @classmethod
    def set(cls, key: str, value: str) -> str:
        env = cls.normalize_key(key)
        cls.update({env: value})
        return env

    @classmethod
    def unset(cls, key: str) -> bool:
        env = cls.normalize_key(key)
        # A local key can be removed even while portable settings are unavailable.
        from interact.server_tool_settings import PORTABLE_ENV  # circular Config/UserConfig wiring

        data = cls.read() if env in PORTABLE_ENV and cls.server() else cls.read_local()
        existed = env in data
        cls.update({env: None})
        return existed

    @classmethod
    def apply(cls, *, portable: bool = True) -> None:
        """Load persisted settings into ``os.environ`` without overriding live vars."""
        cls.process_interact_env()
        from interact.server_tool_settings import PORTABLE_ENV  # circular Config/UserConfig wiring

        connected = cls.server() is not None
        if connected:
            for name in PORTABLE_ENV:
                os.environ.pop(name, None)
        data = cls.read() if portable else cls.read_local()
        for name, value in data.items():
            if connected and not portable and name in PORTABLE_ENV:
                continue
            os.environ.setdefault(name, value)

    @classmethod
    def _write(cls, data: dict[str, str]) -> None:
        cls.PATH.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(
            f"{name}={_quote_if_needed(value)}\n" for name, value in sorted(data.items())
        )
        cls.PATH.write_text(body)
        cls.PATH.chmod(0o600)
