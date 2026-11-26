"""Typed conversation contract shared by the local host and generated TypeScript bindings."""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictStr,
    model_validator,
)
from galaius_core import AGENT_IMAGES_MAX, PromptSelection

from galaius.agents.limits import quota
from galaius.agents.transport.events import (
    AgentEvent,
    ConversationInteraction,
    InteractionField,
)
from galaius.agents.registry import (
    AgentRun,
    ChargePath,
    ConnectionMode,
    ConversationCapability,
    CostCertainty,
)
from galaius.catalog.criteria import Criteria, CriteriaError
from galaius.models.registry import Model


RouteAvailability = Literal[
    "available", "unavailable", "unauthenticated", "incompatible", "policy_blocked"
]
_ENTITY_ID = r"^[A-Za-z0-9._:@+-]+$"
_MODEL_ID = r"^[A-Za-z0-9._:/@+-]+$"
ConversationMethod = Literal["initialize", "catalog", "start", "send", "cancel", "compact", "interaction"]
ConversationErrorCode = Literal[
    "invalid_request",
    "unsupported_version",
    "unavailable",
    "unauthenticated",
    "incompatible",
    "not_found",
    "conflict",
    "provider_failed",
    "cancelled",
    "internal_error",
]


class ModelSelection(BaseModel):
    """Unresolved user intent: one exact model, one criterion, or the route default."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, max_length=256, pattern=_MODEL_ID)
    criterion: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def one_intent(self) -> Self:
        if self.model is not None and self.criterion is not None:
            raise ValueError("model and criterion are mutually exclusive")
        if self.model is not None and not self.model.strip():
            raise ValueError("model must not be empty")
        if self.criterion is not None and not self.criterion.strip():
            raise ValueError("criterion must not be empty")
        return self


class ConversationRequest(BaseModel):
    """A validated first turn; continuation inherits its persisted route and model."""

    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(min_length=1, max_length=160, pattern=_ENTITY_ID)
    #: What he asks, whole: never capped (a turn past what it takes in reaches its agent as a file).
    prompt: str = ""
    #: The image files the turn shows (absolute paths), kept as the conversation's own (`RunMedia`).
    media: tuple[str, ...] = Field(default=(), max_length=AGENT_IMAGES_MAX)
    prompt_selection: PromptSelection | None = None
    selection: ModelSelection = Field(default_factory=ModelSelection)
    workspace_root: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def meaningful_prompt(self) -> Self:
        if not self.prompt.strip() and not self.media:
            raise ValueError("prompt must not be blank")
        return self


class ConversationRoute(BaseModel):
    """One live provider + connection pairing and the models it can run now."""

    id: str = Field(min_length=1, max_length=160, pattern=_ENTITY_ID)
    provider: str = Field(min_length=1, max_length=80)
    connection: ConnectionMode
    label: str = Field(min_length=1, max_length=160)
    availability: RouteAvailability
    reason: str = Field(default="", max_length=500)
    charge_path: ChargePath
    cost_certainty: CostCertainty
    billing_note: str = Field(max_length=500)
    authenticated: bool | None = None
    capabilities: list[ConversationCapability] = Field(default_factory=list)
    models: list[Model] = Field(default_factory=list)
    default_model: str | None = None
    cataloged_at: float

    def model_by_id(self, model_id: str) -> Model | None:
        return next((model for model in self.models if model.id == model_id), None)

    def resolve(self, selection: ModelSelection) -> Model:
        """Resolve one exact model or route-bound criterion without crossing routes — skipping
        anything `quota.shut` still holds, so a FRESH start and a RESUMED turn never
        re-offer a model that already refused for quota a moment ago. This is the ONE place both
        call it (`host.py`'s `_start` and `_send`), so both share the fix rather than one route
        re-implementing the walk and drifting from the other.
        """
        if self.availability != "available":
            raise ValueError(self.reason or f"route {self.id!r} is unavailable")
        cooled_until = {
            model.id: entry.until for model in self.models
            if (entry := quota.shut(self.provider, model.id)) is not None and entry.until is not None
        }
        live = [model for model in self.models if model.id not in cooled_until]
        if selection.model is not None:
            model = self.model_by_id(selection.model)
            if model is None:
                raise ValueError("the selected model is no longer available on this route")
            if model.id not in cooled_until:
                return model
            # Cooled: the route's next model with usage takes it (owner 2026-11-20 « If fable is not available it should
            # fallback »); none with usage: the vendor is asked about the one whose note ends first.
            return live[0] if live else self._earliest(cooled_until)
        if selection.criterion is not None:
            try:
                criterion = Criteria.parse(selection.criterion)
            except CriteriaError as exc:
                raise ValueError(str(exc)) from exc
            model = criterion.choose(available_only=False, candidates=live)
            if model is None:
                if self.models and not live:
                    return self._earliest(cooled_until)
                raise ValueError("no model on the selected route clears this criterion")
            return model
        if self.default_model is not None:
            model = self.model_by_id(self.default_model)
            if model is not None and model.id not in cooled_until:
                return model
        if not live:
            if cooled_until:
                return self._earliest(cooled_until)
            raise ValueError("the selected route currently exposes no runnable model")
        return live[0]

    def _earliest(self, cooled_until: dict[str, float]) -> Model:
        """No model of this route is free by this PC's notes: a note is a forecast, so the vendor is asked about the model whose
        note ends first — a turn is never refused here while a model could answer it."""
        return self.models[[model.id for model in self.models].index(min(cooled_until, key=cooled_until.__getitem__))]


class ConversationCatalog(BaseModel):
    """The account-scoped routes, models, and criterion vocabulary available right now."""

    version: Literal[1] = 1
    routes: list[ConversationRoute]
    criteria: list[str]
    cataloged_at: float

    def route_by_id(self, route_id: str) -> ConversationRoute | None:
        return next((route for route in self.routes if route.id == route_id), None)

    def resolve(self, request: ConversationRequest) -> tuple[ConversationRoute, Model]:
        route = self.route_by_id(request.route_id)
        if route is None:
            raise ValueError("the selected route does not exist in the current catalog")
        return route, route.resolve(request.selection)


class ConversationCommandBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class InitializeCommand(ConversationCommandBase):
    method: Literal["initialize"]


class CatalogCommand(ConversationCommandBase):
    method: Literal["catalog"]


class StartCommand(ConversationCommandBase):
    method: Literal["start"]
    request: ConversationRequest


class SendCommand(ConversationCommandBase):
    method: Literal["send"]
    run_id: str = Field(min_length=1, max_length=160, pattern=_ENTITY_ID)
    prompt: str = ""
    #: As `ConversationRequest.media`.
    media: tuple[str, ...] = Field(default=(), max_length=AGENT_IMAGES_MAX)

    @model_validator(mode="after")
    def meaningful_prompt(self) -> Self:
        if not self.prompt.strip() and not self.media:
            raise ValueError("prompt must not be blank")
        return self


class CancelCommand(ConversationCommandBase):
    method: Literal["cancel"]
    run_id: str = Field(min_length=1, max_length=160, pattern=_ENTITY_ID)


class CompactCommand(ConversationCommandBase):
    """Have the provider replace the conversation so far with its summary, between turns."""

    method: Literal["compact"]
    run_id: str = Field(min_length=1, max_length=160, pattern=_ENTITY_ID)


class InteractionSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interaction_id: str = Field(min_length=1, max_length=160, pattern=_ENTITY_ID)
    values: dict[str, StrictStr | StrictBool]


class InteractionCommand(ConversationCommandBase):
    method: Literal["interaction"]
    run_id: str = Field(min_length=1, max_length=160, pattern=_ENTITY_ID)
    submission: InteractionSubmission


ConversationCommandValue = Annotated[
    InitializeCommand | CatalogCommand | StartCommand | SendCommand
    | CancelCommand | CompactCommand | InteractionCommand,
    Field(discriminator="method"),
]


class ConversationCommand(RootModel[ConversationCommandValue]):
    """One finite input envelope; its method selects exactly one parameter shape."""


class InitializeResponse(BaseModel):
    version: Literal[1]
    type: Literal["response"]
    method: Literal["initialize"]
    request_id: str
    ok: Literal[True]
    methods: list[ConversationMethod]


class CatalogResponse(BaseModel):
    version: Literal[1]
    type: Literal["response"]
    method: Literal["catalog"]
    request_id: str
    ok: Literal[True]
    catalog: ConversationCatalog


class RunResponse(BaseModel):
    version: Literal[1]
    type: Literal["response"]
    method: Literal["start", "send", "cancel", "compact", "interaction"]
    request_id: str
    ok: Literal[True]
    run: AgentRun


class ErrorResponse(BaseModel):
    version: Literal[1]
    type: Literal["response"]
    method: ConversationMethod | None = None
    request_id: str
    ok: Literal[False]
    error_code: ConversationErrorCode
    error: str = Field(max_length=500)


ConversationResponseValue = InitializeResponse | CatalogResponse | RunResponse | ErrorResponse


class ConversationResponse(RootModel[ConversationResponseValue]):
    """A finite response envelope: success payload or bounded typed error, never both."""


class ConversationStreamEvent(BaseModel):
    version: Literal[1]
    type: Literal["event"]
    run: AgentRun
    event: AgentEvent


__all__ = [
    "CancelCommand",
    "CatalogCommand",
    "CatalogResponse",
    "ChargePath",
    "CompactCommand",
    "ConnectionMode",
    "ConversationCapability",
    "ConversationCatalog",
    "ConversationCommand",
    "ConversationCommandBase",
    "ConversationErrorCode",
    "ConversationInteraction",
    "ConversationMethod",
    "ConversationRequest",
    "ConversationResponse",
    "ConversationRoute",
    "ConversationStreamEvent",
    "CostCertainty",
    "ErrorResponse",
    "InitializeCommand",
    "InitializeResponse",
    "InteractionCommand",
    "InteractionField",
    "InteractionSubmission",
    "ModelSelection",
    "RouteAvailability",
    "RunResponse",
    "SendCommand",
    "StartCommand",
]
