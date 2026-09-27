"""The user-facing ``interact`` command surface, grouped into a package.

This is the CLI a person runs (``interact``, ``interact status``, ``interact config`` …) and the
bare-``interact`` config TUI — NOT the MCP tool surface (that is :mod:`interact.server`).
``app`` holds the cyclopts app + every command + ``main`` (the ``interact`` entry point);
``tui`` / ``clients`` / ``usage`` / ``view`` / ``render`` / ``update`` are its helpers, imported
only within this cluster.

``main`` is re-exported so the ``interact = "interact.cli:main"`` entry point resolves. Command
implementations stay deferred until selected; package-level command attributes are resolved lazily
for existing callers. The ``usage`` / ``update`` command functions are deliberately NOT exposed
here because they would shadow the matching submodules.
"""

from interact.cli.app import __getattr__ as _resolve_command
from interact.cli.app import app, main, version


def __getattr__(name: str):
    return _resolve_command(name)


__all__ = ["app", "main", "version"]
