"""The ``galaius`` command a person runs (``galaius``, ``galaius status``, ``galaius config`` …) and the
bare-``galaius`` config TUI, not the MCP tool surface (:mod:`galaius.server`). ``app`` holds the
cyclopts app and ``main`` (the ``galaius`` entry point); ``app_commands`` the command bodies, loaded
when a command is selected; ``tui`` / ``clients`` / ``usage`` / ``view`` / ``render`` / ``update``
are its helpers."""
