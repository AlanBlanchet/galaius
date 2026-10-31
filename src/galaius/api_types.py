"""Public type aggregator for TypeScript codegen.

`pydantic2ts --module galaius.api_types` walks this module and emits
the union of all Pydantic models referenced in ``__all__``. Anything not
listed here will NOT appear in the generated TS bindings.

Keep this file flat — no behavior, just re-exports.
"""

from galaius.agents.transport.events import AgentEvent, ConversationInteraction, InteractionField
from galaius.agents.transport.protocol import (
    CancelCommand,
    CatalogCommand,
    CatalogResponse,
    CompactCommand,
    ConversationCatalog,
    ConversationCommand,
    ConversationRequest,
    ConversationResponse,
    ConversationRoute,
    ConversationStreamEvent,
    ErrorResponse,
    InitializeCommand,
    InitializeResponse,
    InteractionCommand,
    InteractionSubmission,
    ModelSelection,
    RunResponse,
    SendCommand,
    StartCommand,
)
from galaius.agents.registry import AgentRun
from galaius.catalog.published import PublishedEntry, PublishedTable
from galaius.catalog.upstream import UpstreamSource
from galaius.vision.formats import BoxOrder, CoordFormat
from galaius.models.registry import Benchmark, BenchmarkRecommendation, Model
from galaius.models.capability import ModelCapability
from galaius.models.spec import ModelsConfig, ModelSpec, ProviderSpec
from galaius.vision.usage_records import UsageEntry

__all__ = [
    "AgentEvent",
    "AgentRun",
    "Benchmark",
    "BenchmarkRecommendation",
    "BoxOrder",
    "CancelCommand",
    "CatalogCommand",
    "CompactCommand",
    "CatalogResponse",
    "ConversationCatalog",
    "ConversationCommand",
    "ConversationInteraction",
    "ConversationRequest",
    "ConversationResponse",
    "ConversationRoute",
    "ConversationStreamEvent",
    "CoordFormat",
    "ErrorResponse",
    "InitializeCommand",
    "InitializeResponse",
    "InteractionCommand",
    "InteractionField",
    "InteractionSubmission",
    "Model",
    "ModelCapability",
    "ModelSelection",
    "ModelSpec",
    "ModelsConfig",
    "ProviderSpec",
    "PublishedEntry",
    "PublishedTable",
    "RunResponse",
    "SendCommand",
    "StartCommand",
    "UpstreamSource",
    "UsageEntry",
]
