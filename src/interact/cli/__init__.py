"""The user-facing ``interact`` command surface, grouped into a package.

This is the CLI a person runs (``interact``, ``interact status``, ``interact config`` …) and the
bare-``interact`` config TUI — NOT the MCP tool surface (that is :mod:`interact.server`).
``app`` holds the cyclopts app + every command + ``main`` (the ``interact`` entry point);
``tui`` / ``clients`` / ``usage`` / ``view`` / ``render`` / ``update`` are its helpers, imported
only within this cluster.

``main`` is re-exported so the ``interact = "interact.cli:main"`` entry point resolves, along with
the commands + helpers tests reach via ``import interact.cli``. The ``usage`` / ``update`` command
functions are deliberately NOT re-exported here — they would shadow the ``interact.cli.usage`` /
``interact.cli.update`` submodules; reach them via ``app`` (they are registered as commands there).
"""

from interact.cli.app import (  # noqa: F401
    _mask,
    _print_resolved_models,
    _print_stale_servers,
    app,
    config_get,
    config_list,
    config_path,
    config_set,
    config_unset,
    dashboard,
    doctor,
    install,
    main,
    mcp,
    providers,
    report,
    status,
    version,
)
