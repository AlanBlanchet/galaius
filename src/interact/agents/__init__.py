"""Spawning and supervising agent runs across vendors.

interact is an MCP *server*; every agent CLI it supervises is an MCP *client*. That is why
cross-provider teamwork needs no bespoke bus — handing a spawned agent interact's own server
config lets a Claude agent call the same tools a Codex agent calls, and MCP is vendor-neutral.

The boundary that keeps this legal is in :mod:`interact.agents.providers`: interact spawns the
vendor's own binary and reads stdout; it never touches a credential.
"""

from interact.agents.events import AgentEvent, EventKind
from interact.agents.providers import (
    PROVIDERS,
    AgentProvider,
    ClaudeCodeProvider,
    CodexProvider,
    available_providers,
    provider_for,
)

__all__ = [
    "PROVIDERS", "AgentEvent", "AgentProvider", "ClaudeCodeProvider", "CodexProvider",
    "EventKind", "available_providers", "provider_for",
]
