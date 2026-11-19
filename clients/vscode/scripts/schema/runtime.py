"""Export Pydantic conversation wire schemas for the TypeScript runtime decoder."""

import json

from pydantic import TypeAdapter

from galaius.agents.transport.events import AgentEvent
from galaius.agents.transport.protocol import (
    ConversationCommand,
    ConversationResponse,
    ConversationStreamEvent,
)
from galaius.agents.registry import AgentRun

SCHEMAS = {
    "ConversationCommand": TypeAdapter(ConversationCommand).json_schema(),
    "ConversationResponse": TypeAdapter(ConversationResponse).json_schema(),
    "ConversationStreamEvent": TypeAdapter(ConversationStreamEvent).json_schema(),
    "AgentRun": TypeAdapter(AgentRun).json_schema(),
    "AgentEvent": TypeAdapter(AgentEvent).json_schema(),
}

print(json.dumps(SCHEMAS, separators=(",", ":"), sort_keys=True))
