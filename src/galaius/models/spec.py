"""Model and provider configuration: which model plays which role, and where its provider lives."""

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    pass


ModelRole = Literal["image", "component", "video", "audio"]


class ModelSpec(BaseModel):
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    #: Prompt tokens served FROM the provider's prompt cache, and tokens WRITTEN to it (litellm's
    #: `cache_read_input_token_cost` / `cache_creation_input_token_cost`, default cache lifetime).
    #: None = the source quotes no cache rate for this model, never "same as input".
    cache_read_cost_per_million: float | None = None
    cache_write_cost_per_million: float | None = None
    supports_response_schema: bool = False
    #: How strong the model is, on the catalog's own scale. Present in models.json all along,
    #: dropped on load — why nothing at runtime could order a chain by quality.
    intelligence_score: float | None = None
    # Capability tags carried from upstream catalog (litellm) into models.json, so a model's
    # behaviour (e.g. grounding default) is DERIVED from a live source, not a hardcoded list.
    # See generate-models.py for how these are sourced.
    capabilities: list[str] = Field(default_factory=list)
    #: What the model PRODUCES, in litellm's words (chat, responses, image_generation,
    #: video_generation, embedding, audio_speech, audio_transcription, realtime); None = unknown.
    mode: str | None = None


class ProviderSpec(BaseModel):
    model_config = {"populate_by_name": True}

    env_keys: list[str] = Field(default_factory=list, alias="envKeys")
    models: dict[str, ModelSpec] = {}


class ModelsConfig(BaseModel):
    """Typed schema for models.json content."""

    model_config = {"populate_by_name": True}

    providers: dict[str, ProviderSpec] = {}
    recommendations: dict[str, list[str]] = {}
    coord_formats: dict[str, dict] = Field(default_factory=dict, alias="coordFormats")
    defaults: dict[str, str] = {}
