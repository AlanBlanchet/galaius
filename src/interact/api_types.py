"""Public type aggregator for TypeScript codegen.

`pydantic2ts --module interact.api_types` walks this module and emits
the union of all Pydantic models referenced in ``__all__``. Anything not
listed here will NOT appear in the generated TS bindings.

Keep this file flat — no behavior, just re-exports.
"""

from interact.agents.events import AgentEvent, ConversationInteraction, InteractionField
from interact.agents.protocol import (
    CancelCommand,
    CatalogCommand,
    CatalogResponse,
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
from interact.agents.registry import AgentRun
from interact.benchmarks.published import PublishedEntry, PublishedTable
from interact.benchmarks.upstream import UpstreamSource
from interact.formats import BoxOrder, CoordFormat
from interact.models import (
    Benchmark,
    BenchmarkRecommendation,
    Model,
    ModelCapability,
    ModelsConfig,
    ModelSpec,
    ProviderSpec,
)
from interact.vision.usage_records import UsageEntry

__all__ = [
    "AgentEvent",
    "AgentRun",
    "Benchmark",
    "BenchmarkRecommendation",
    "BoxOrder",
    "CancelCommand",
    "CatalogCommand",
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
