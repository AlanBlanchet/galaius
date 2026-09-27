"""Export Pydantic conversation wire schemas for the TypeScript runtime decoder."""

import json

from pydantic import TypeAdapter

from interact.agents.events import AgentEvent
from interact.agents.protocol import (
    ConversationCommand,
    ConversationResponse,
    ConversationStreamEvent,
)
from interact.agents.registry import AgentRun

SCHEMAS = {
    "ConversationCommand": TypeAdapter(ConversationCommand).json_schema(),
    "ConversationResponse": TypeAdapter(ConversationResponse).json_schema(),
    "ConversationStreamEvent": TypeAdapter(ConversationStreamEvent).json_schema(),
    "AgentRun": TypeAdapter(AgentRun).json_schema(),
    "AgentEvent": TypeAdapter(AgentEvent).json_schema(),
}

print(json.dumps(SCHEMAS, separators=(",", ":"), sort_keys=True))
