"""Building and swapping a `Model` catalog for tests — the `models.json` shape `Model.load_registry`
reads, and a live in-memory registry swap. Lives under `tests/support/`, not as a bare
`tests/model_catalog.py`, so every importer reaches it the same way a test reaches any other
shared fixture, instead of one test module importing a name out of another."""

import copy
import json
from contextlib import contextmanager

from interact.models import Model, ModelCapability


def model(
    id: str = "test/model",
    *,
    provider: str | None = None,
    caps: set[ModelCapability] | None = None,
    input_cost: float | None = 0.0,
    output_cost: float | None = 0.0,
    score: float | None = None,
) -> Model:
    """A `Model` with the usual defaults. Three files each rebuilt this (`_make_model`, `_model`,
    `_row`) with drifted defaults and one real per-scenario difference: grounding tests want a
    bare `id` with only capabilities declared; ranking tests want a `provider` distinct from a
    bare (no-`/`) model id, plus an intelligence `score`. `provider` defaults to the part of `id`
    before `/` (its usual shape), or `id` itself when there is none, so both callers reach it the
    same way. A dead `available` parameter one caller declared but never used or passed through
    is dropped here."""
    return Model(
        id=id,
        provider=provider or id.partition("/")[0] or id,
        capabilities=set(caps) if caps is not None else {ModelCapability.VLM},
        input_cost_per_million=input_cost,
        output_cost_per_million=output_cost,
        intelligence_score=score,
    )


@contextmanager
def catalog_of(*models: Model):
    """Swap the catalog for exactly these models, then put back what was there. Later tests — and
    the self-loading `Model.catalog()` — read the same registry, and an emptied one is not
    "unloaded": it is a catalog that says there are no models."""
    saved = list(Model.registry())
    # The loader's own state is part of the catalog: a test that LOADS a fixture JSON overwrites
    # the provider keys / grounding table every later test reads, so they go back too.
    loader_state = {
        name: copy.copy(getattr(Model, name))
        for name in ("_provider_keys", "_component_recommendations", "_coord_formats", "_served")
    }
    Model._reset()
    for model in models:
        Model._register(model)
    try:
        yield
    finally:
        Model._registry[:] = saved
        for name, value in loader_state.items():
            setattr(Model, name, value)


def catalog_dict(
    *models: str | tuple[str, float, float],
    env_keys: dict[str, list[str]] | None = None,
    recommend: dict[str, list[str]] | None = None,
    coord_formats: dict[str, dict] | None = None,
    extra: dict[str, dict] | None = None,
    defaults: dict | None = None,
) -> dict:
    """The `{"providers": {...}, "recommendations": {...}, "coordFormats": {...}}` shape
    `Model.load_registry` reads, built from a flat model list instead of hand-nesting JSON —
    four call sites each rebuilt this by hand with one or two real differences (extra per-model
    fields, which recommendation category, one provider vs several).

    Each entry is a bare model id (cost 0/0) or `(id, input_cost, output_cost)`; its provider is
    the part of the id before `/`. `env_keys` maps provider -> required env var names — a
    provider absent from it gets `[]`, i.e. never "configured" (a keyless subscription wrapper
    needs no entry). `extra` merges additional per-model fields (`capabilities`,
    `intelligence_score`, ...) by model id. `recommend` is the `recommendations` block passed
    through as-is, since its category key varies (`"component"`, `"image"`, ...). `defaults`, if
    given, becomes a top-level `"defaults"` key — most catalogs have none."""
    providers: dict[str, dict] = {}
    for entry in models:
        model_id, input_cost, output_cost = entry if isinstance(entry, tuple) else (entry, 0.0, 0.0)
        provider, _, _ = model_id.partition("/")
        bucket = providers.setdefault(
            provider, {"envKeys": (env_keys or {}).get(provider, []), "models": {}}
        )
        bucket["models"][model_id] = {
            "input_cost_per_million": input_cost,
            "output_cost_per_million": output_cost,
            **(extra or {}).get(model_id, {}),
        }
    catalog: dict = {
        "providers": providers,
        "recommendations": recommend or {},
        "coordFormats": coord_formats or {},
    }
    if defaults is not None:
        catalog["defaults"] = defaults
    return catalog


def catalog_json(*args: str | tuple[str, float, float], **kwargs) -> str:
    """`catalog_dict`, serialized — what every call site actually wants: a string for
    `INTERACT_MODELS_JSON` / `Model.load_registry(...)`."""
    return json.dumps(catalog_dict(*args, **kwargs))
