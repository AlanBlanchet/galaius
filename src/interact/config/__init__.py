"""Configuration subsystem, grouped into a package.

One concern — how interact is configured — split by cohesion: ``settings`` (the :class:`Config`
``BaseSettings` model + the session/model-resolution helpers), ``user`` (the
``~/.interact/config.env`` persistence store), ``schema`` (the one declarative ``SETTINGS`` list
every front end renders from) and ``dotenv`` (the CLI/test ``.env`` loader). This ``__init__``
re-exports the whole public surface, so ``from interact.config import Config`` (and now
``UserConfig`` / ``SETTINGS`` / ``load_dotenv_for_cli``) all resolve from the one config namespace.

The live ``config`` singleton lives in :mod:`interact.runtime` (kept top-level: it is app runtime
wiring, imported ~20×, and depends on this package rather than belonging inside it).
"""

from interact.config.settings import (  # noqa: F401
    DEFAULT_LIMIT,
    LOG_MAXLEN,
    QUALITY_TIERS,
    _DEFAULT_SOVEREIGN_MODEL,
    _SOVEREIGN_MODELS,
    _resolve_session_name,
    _safe_dir_name,
    _session_custom_title,
    Config,
    caller_session_name,
)
from interact.config.user import UserConfig  # noqa: F401
from interact.config.schema import (  # noqa: F401
    _ROLE_CAP,
    SETTINGS,
    Option,
    Setting,
    SettingGroup,
    SettingKind,
    by_key,
    groups,
    to_json_dict,
)
from interact.config.dotenv import load_dotenv_for_cli  # noqa: F401
