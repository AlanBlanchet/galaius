"""Configuration subsystem, grouped into a package.

One concern — how galaius is configured — split by cohesion: ``settings`` (the :class:`Config`
``BaseSettings` model + the session/model-resolution helpers), ``user`` (the
``~/.galaius/config.env`` persistence store), ``schema`` (the one declarative ``SETTINGS`` list
every front end renders from) and ``dotenv`` (the CLI/test ``.env`` loader). This ``__init__``
re-exports the whole public surface, so ``from galaius.config import Config`` (and now
``UserConfig`` / ``SETTINGS`` / ``load_dotenv_for_cli``) all resolve from the one config namespace.

The live ``config`` singleton lives in :mod:`galaius.runtime` (kept top-level: it is app runtime
wiring, imported ~20×, and depends on this package rather than belonging inside it).
"""

from galaius.config.settings import (  # noqa: F401
    DEFAULT_LIMIT,
    LOG_MAXLEN,
    QUALITY_TIERS,
    _resolve_session_name,
    _safe_dir_name,
    _session_custom_title,
    Config,
    caller_session_name,
)
from galaius.config.user import UserConfig  # noqa: F401
from galaius.config.schema import (  # noqa: F401
    SETTINGS,
    Option,
    Setting,
    SettingGroup,
    SettingKind,
    by_key,
    groups,
    to_json_dict,
)
from galaius.config.dotenv import load_dotenv_for_cli  # noqa: F401
