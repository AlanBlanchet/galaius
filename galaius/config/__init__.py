"""How galaius is configured: ``settings`` (the ``Config`` model and the session / model resolution
helpers), ``user`` (the ``~/.galaius/config.env`` store), ``schema`` (the declarative ``SETTINGS`` list
every front end renders from) and ``dotenv`` (the CLI / test ``.env`` loader). The live ``config``
singleton lives in :mod:`galaius.runtime`."""
