"""Apply persisted CLI configuration before importing command dependencies."""

import sys

from galaius.config import Config, UserConfig, load_dotenv_for_cli
from galaius.server_tool_settings import MachineNotice


def apply_command_environment() -> None:
    """Apply persisted command configuration before deferred dependencies load or execute."""
    # Configuration commands must be able to report auth errors and edit local keys offline.
    # Portable server preferences are resolved by their adapter or runtime invocation refresh.
    UserConfig.apply(portable=False)
    load_dotenv_for_cli()
    MachineNotice.say(sys.stderr)


apply_command_environment()

__all__ = ["Config", "UserConfig", "apply_command_environment"]
