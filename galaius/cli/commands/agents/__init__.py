"""The `agents` command group: importing the package registers every command on `agents_app`."""

from galaius.cli.commands.agents import catalog, conversations, models, providers, runs, spawn  # noqa: F401  (each module's decorators add its commands)
from galaius.cli.commands.agents.app import agents_app

__all__ = ["agents_app"]
