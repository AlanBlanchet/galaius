"""Model metadata that is CURRENT, and that says how current it is.

``data/models.json`` is a snapshot: context/prices age silently, unreadable as stale. Fetches
OpenRouter's public catalog (unauthenticated GET, no signup/key); falls back to LiteLLM's MIT
static file, already a dependency.

Not Artificial Analysis: free tier needs a per-user key, terms are "internal use only, no
redistribution" — an open-source tool can't bundle a key or ship its scores. Nothing here needs
a credential.

Invariant: **the catalog always carries its source and its age.** Stale data is fine when the
network is down; serving it AS live is the bug being fixed.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from galaius.ttl_cache import TTL_SECONDS, RefreshFailed, TTLCache, age_of, describe_age

_log = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
_FETCH_TIMEOUT = 6.0


@dataclass(frozen=True)
class ModelInfo:
    """What a caller actually needs to choose or price a model."""

    id: str
    name: str = ""
    context_length: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    input_modalities: tuple[str, ...] = ()
    #: Artificial Analysis capability score, when the registry knows one. Carried here so a
    #: picker row can show measured competence, not just name.
    intelligence_score: float | None = None


@dataclass(frozen=True)
class Catalog:
    """Models plus the provenance a UI must show alongside them."""

    models: list[ModelInfo]
    source: str  # "openrouter" | "litellm" | "bundled"
    fetched_at: float

    @property
    def age_seconds(self) -> float:
        return age_of(self.fetched_at)

    @property
    def is_live(self) -> bool:
        """True only for network data within its TTL. A UI showing a price should show this
        too — else it repeats the problem this module exists to fix."""
        return self.source == "openrouter" and self.age_seconds <= TTL_SECONDS

    def describe(self) -> str:
        return f"{len(self.models)} models · {self.source} · {describe_age(self.age_seconds)}"


#: Beside the agent registry, fixed path — CLI and extension both read it, must not move with
#: ``GALAIUS_DEBUG_DIR``.
CACHE = TTLCache("model_catalog.json", TTL_SECONDS)


def cache_path() -> Path:
    return CACHE.path


def _num(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _from_openrouter(payload: dict) -> list[ModelInfo]:
    out: list[ModelInfo] = []
    for raw in payload.get("data") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue  # a malformed entry must not lose the rest of the catalog
        pricing = raw.get("pricing") or {}
        arch = raw.get("architecture") or {}
        out.append(ModelInfo(
            id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            context_length=raw.get("context_length") if isinstance(raw.get("context_length"), int) else None,
            input_cost_per_token=_num(pricing.get("prompt")),
            output_cost_per_token=_num(pricing.get("completion")),
            input_modalities=tuple(arch.get("input_modalities") or ()),
        ))
    return out


def _from_litellm() -> list[ModelInfo]:
    """LiteLLM ships the same facts as a static MIT map, already a dependency — offline path
    needs no vendored copy of our own."""
    try:
        import litellm

        data = getattr(litellm, "model_cost", None) or {}
    except Exception:
        return []
    out: list[ModelInfo] = []
    for model_id, raw in data.items():
        if not isinstance(raw, dict):
            continue
        modalities = ["text"]
        if raw.get("supports_vision"):
            modalities.append("image")
        if raw.get("supports_audio_input"):
            modalities.append("audio")
        out.append(ModelInfo(
            id=str(model_id),
            name=str(model_id),
            context_length=raw.get("max_input_tokens") if isinstance(raw.get("max_input_tokens"), int) else None,
            input_cost_per_token=_num(raw.get("input_cost_per_token")),
            output_cost_per_token=_num(raw.get("output_cost_per_token")),
            input_modalities=tuple(modalities),
        ))
    return out


#: Everything a RESELLER or release process appends, any order — applied to a fixpoint by
#: `bare_model_name` so stacking them in any sequence still collapses to one name.
_SUFFIXES = (
    r"@.*$",                                  # revision pin: @default, @20251001
    r"[-@:]?20\d{6}$",                        # release date: -20250929
    r"[-@:]?20\d{2}-\d{2}-\d{2}$",            # ...spelled with dashes
    r":[a-z0-9][a-z0-9._-]*$",                # hosting tag: :cloud :free :0
    r"-v\d+$",                                # Bedrock-style revision: -v1
    r"-(?:0[1-9]|1[0-2])\d{2}$",               # trailing MMDD revision: -0309
    r"-0\d{2}$",                              # Google-style revision: -002
    r"-(?:latest|preview|exp)$",              # pointer / maturity suffix
)


def bare_model_name(model_id: str) -> str:
    """The model's own name, without whoever resells it. `azure/gpt-5.5`,
    `us.anthropic.claude-opus-4-7`, `vertex_ai/claude-opus-4-7@default` and
    `openrouter/anthropic/claude-opus-4.7` are one model wearing four resellers' clothes; a
    version written `4.7` or `4-7` is the same version."""
    if model_id.startswith("ft:"):
        # A fine-tune IS its own model: stripping its `:`-separated parts left every one as
        # "ft", collapsing unrelated fine-tunes onto one row.
        return model_id.lower().replace(".", "-")
    name = model_id.rsplit("/", 1)[-1].lower()
    name = re.sub(r"\s*\[[^\]]*\]\s*$", "", name)             # context variant: [1m]
    name = re.sub(r"^[a-z]{2,6}\.(?=[a-z])", "", name)          # region: us. eu. apac. global.
    vendorless = re.sub(r"^[a-z0-9_-]+\.(?=[a-z])", "", name)   # vendor: anthropic. meta.
    # ...unless what's left is a bare version — that prefix WAS the name (`deepseek.v3.2` is
    # not `v3.2`; `mistral.v3.2` would collide with it).
    if not re.match(r"^v?\d", vendorless):
        name = vendorless
    # Maturity label sits mid-id as often as it ends one (`grok-4.20-beta-0309-reasoning`); what
    # genuinely names a different model — `reasoning`, `non-reasoning`, `multi-agent`, `image` —
    # is never touched.
    name = re.sub(r"-(?:beta|preview|exp|rc)(?=-|$)", "", name)
    # Mid-id MMDD revision (`grok-4.20-0309-reasoning`), month-checked so a plain four-digit run
    # (`qwen-3-4096-instruct`) keeps its meaning.
    name = re.sub(r"-(?:0[1-9]|1[0-2])\d{2}(?=-[a-z])", "", name)
    # Every SUFFIX rule in one fixpoint, so ORDER can't matter. Used to run once each in fixed
    # sequence — a date behind a hosting tag (`...-20250929-v1:0`) was never reached: 74 real
    # litellm ids kept their date, invisible to every score lookup.
    while True:
        shorter = name
        for pattern in _SUFFIXES:
            shorter = re.sub(pattern, "", shorter)
        if shorter == name or not shorter:
            break
        name = shorter
    return name.replace(".", "-")


def leaderboard_path() -> Path | None:
    """Where the live Artificial Analysis fetch lands. VS Code panel's Benchmarks tab reads
    this exact file; so must anything printing an `aa.` number, or the two disagree in public.

    `BENCHMARK_SCORES` overrides it; EMPTY means "no board" — registry loads at import time, so
    a dev box's real fetch would otherwise give tests different scores than CI. Not
    `GALAIUS_`-prefixed, same reason as `OLLAMA_DISCOVERY`: a config refresh drops every
    `GALAIUS_*` the config file doesn't define.
    """
    override = os.environ.get("BENCHMARK_SCORES")
    if override is not None:
        return Path(override) if override else None
    return Path.home() / ".galaius" / "out" / "benchmark_scores.json"


def _leaderboard_key(name: str) -> str:
    """A board row's display name reduced to the model it names. The board dresses each model
    in its effort level — `GPT-5.5 (xhigh)`, `Claude Fable 5.1 (Adaptive Reasoning, Max
    Effort)` — a setting, not a different model, so the parenthetical goes.

    A SLUG, not the final key: `live_scores` then runs `bare_model_name` over this, which puts
    a board NAME and a catalog ID in one space. Naming a function that no longer exists
    (`_bare_model_name`) hid that second step; the join drifted apart over a maturity word
    until "Gemini 3.1 Pro Preview" and `gemini/gemini-3.1-pro-preview` stopped meeting."""
    stem = re.sub(r"\s*\(.*$", "", name).strip().lower()
    return re.sub(r"[\s.]+", "-", stem)


#: What litellm charges for models the BOARD ranks. Third file beside prices/scores, same TTL,
#: same reason — also keeps a ~2.5s litellm import off every CLI command's path
#: (`galaius.models` loads on all of them).
_RANKED_CACHE = TTLCache("ranked_models.json")
#: How long an EMPTY answer stands. Short on purpose — empty means a source was missing, not
#: nothing to find; a day of silence made the old cache version dangerous.
_EMPTY_RETRY_SECONDS = 15 * 60
#: litellm's `mode` for the models an agent can actually be pointed at.
_CONVERSATIONAL = {"chat", "responses"}


def ranked_extras() -> dict[str, dict]:
    """litellm's price row for every model the live board ranks, keyed by canonical id.

    The catalog a criterion chooses from is a bundled snapshot; the board it's measured against
    is live. Gap isn't cosmetic — snapshot covered 86 of the board's 450 models, so a criterion
    evaluated a fifth of the field and answered with last year's best.

    Nothing invented: litellm already ships these prices, and a ranked model it can't price is
    simply absent, never entering a cheapest-first ordering as though free. Never raises — no
    litellm, no board, or a broken cache all read as "nothing extra": the bundled snapshot alone.
    """
    cached = _RANKED_CACHE.read()
    if cached is not None:
        stored = cached.get("models") or {}
        fresh = age_of(float(cached.get("fetched_at") or 0)) <= TTL_SECONDS
        # An EMPTY answer is never cached for a day. Writing one meant a cold start with no
        # board yet, or a failed litellm import, silently pinned every criterion to the
        # 86-model snapshot until tomorrow — the exact failure this mechanism removes. Empty
        # retries within the hour instead.
        if stored and fresh:
            return stored
        if not stored and age_of(float(cached.get("fetched_at") or 0)) <= _EMPTY_RETRY_SECONDS:
            return {}
    scores = live_scores()
    if not scores:
        _log.warning("no benchmark board on disk: model criteria see only the bundled snapshot")
        _RANKED_CACHE.write({"fetched_at": time.time(), "models": {}})
        return {}
    try:
        import litellm  # lazy on purpose: see the cache note above

        cost = dict(getattr(litellm, "model_cost", None) or {})
    except Exception:
        _log.warning("litellm is unavailable: the board's models cannot be priced, so a criterion "
                     "chooses from the bundled snapshot only")
        cost = {}
    rows: dict[str, dict] = {}
    for model_id, row in cost.items():
        if not isinstance(row, dict):
            continue
        # A model that can't hold a conversation isn't a candidate for an agent, however well
        # the board scores it: embeddings, image generators and rerankers were all eligible.
        if row.get("mode") not in _CONVERSATIONAL:
            continue
        key = bare_model_name(model_id)
        if key not in scores:
            continue
        # Canonical spelling is the one nobody had to qualify: `claude-opus-5` not
        # `au.anthropic.claude-opus-5` — also the vendor's own base price.
        rank = (model_id.count("/") + model_id.count("."), len(model_id))
        keep = rows.get(key)
        if keep is None or rank < (keep["id"].count("/") + keep["id"].count("."), len(keep["id"])):
            rows[key] = dict(row) | {"id": model_id}
    priced = {row["id"]: {k: v for k, v in row.items() if k != "id"} for row in rows.values()}
    _RANKED_CACHE.write({"fetched_at": time.time(), "models": priced})
    return priced


#: Last board read, keyed by file's path/size/mtime. `live_scores` is now asked once per
#: CANDIDATE by percentile criteria, so a re-read per model turned one `explain()` into 19
#: SECONDS. Keyed by file identity so an edited board is picked up next call, not cached for a run.
_BOARD_MEMO: dict[tuple[str, int, float], dict[str, float]] = {}


def live_scores(path: Path | None = None) -> dict[str, float]:
    """What the LIVE board measures, keyed by model name, best effort per model.

    Scores baked into this package are a snapshot that ages the day it ships, and the panel
    already fetches the real board: ranking on the snapshot while calling the number
    `aa.intelligence` put two tabs of one window in open disagreement about which model leads.
    Missing/unreadable board reads as "nothing live", never "nothing scores".
    """
    board = path or leaderboard_path()
    if board is None:
        return {}
    try:
        stat = board.stat()
        key = (str(board), stat.st_size, stat.st_mtime)
        if (memo := _BOARD_MEMO.get(key)) is not None:
            return memo
        raw = json.loads(board.read_text())
    except (OSError, ValueError):
        return {}
    best: dict[str, float] = {}
    for row in raw.get("scores") or []:
        name, value = row.get("name"), row.get("intelligence")
        if not isinstance(name, str) or not name.strip() or not isinstance(value, (int, float)):
            continue
        # ONE normalizer on BOTH sides of the join. `_leaderboard_key` turns a display name
        # into a slug; `bare_model_name` puts that slug in the same space as a model id, what
        # every caller looks up with. Keying by slug alone meant the two never met over a
        # maturity word — "Gemini 3.1 Pro Preview" indexed as `gemini-3-1-pro-preview`, sought
        # as `gemini-3-1-pro` — so that model kept the SNAPSHOT's stale 57.2 while the live
        # board said 30.4, and a top-of-board criterion chose it on a number no board has
        # printed since.
        row_key = bare_model_name(_leaderboard_key(name))
        if row_key and value > best.get(row_key, float("-inf")):
            best[row_key] = float(value)
    # One entry: a changed board gets a new key, old one not worth keeping.
    _BOARD_MEMO.clear()
    _BOARD_MEMO[(str(board), stat.st_size, stat.st_mtime)] = best
    return best


def _with_scores(models: list[ModelInfo]) -> list[ModelInfo]:
    """Same models, each carrying its capability score from the model registry (None if
    nobody measured it). Registry is the one place a score lives; catalog only ferries it."""
    from galaius.models import Model

    by_id = {m.id: m.intelligence_score for m in Model.catalog()}
    return [replace(m, intelligence_score=by_id.get(m.id)) for m in models]


def _read_cache() -> Catalog | None:
    raw = CACHE.read()
    if raw is None:
        return None
    try:
        models = [ModelInfo(
            id=m["id"], name=m.get("name", ""),
            context_length=m.get("context_length"),
            input_cost_per_token=m.get("input_cost_per_token"),
            output_cost_per_token=m.get("output_cost_per_token"),
            input_modalities=tuple(m.get("input_modalities") or ()),
        ) for m in raw["models"]]
    except Exception:
        return None  # a truncated or hand-edited cache is not worth a crash
    if not models:
        return None
    # Scored on READ too: cache written before registry knew a score still shows it.
    return Catalog(models=_with_scores(models), source=raw.get("source", "openrouter"),
                   fetched_at=float(raw.get("fetched_at", 0)))


def _write_cache(catalog: Catalog) -> None:
    CACHE.write({
        "source": catalog.source,
        "fetched_at": catalog.fetched_at,
        "models": [vars(m) | {"input_modalities": list(m.input_modalities)} for m in catalog.models],
    })


def load_catalog(*, refresh: bool = False) -> Catalog:
    """The freshest catalog available, never raising.

    Order: cache still inside TTL, then live fetch, then STALE cache, then LiteLLM's static
    map. Last two marked not-live, so a caller can say so rather than presenting aged numbers
    as current.

    ``refresh`` skips the TTL and re-fetches — what the periodic refresher passes, so a
    long-lived server doesn't keep serving what it read at startup — and raises
    :class:`RefreshFailed` instead of quietly returning the old copy. (Used to be
    ``@lru_cache``d, making any refresher a silent no-op after its first call.)
    """
    cached = _read_cache()
    if not refresh and cached is not None and cached.age_seconds <= TTL_SECONDS:
        return cached
    try:
        fresh = CACHE.refetch("OpenRouter", _fetch)
    except RefreshFailed:
        if refresh:
            raise  # an asked-for refresh that did not happen is said, never passed off as done
        if cached is not None:
            return cached  # stale beats nothing; `is_live` already says it's stale
        return Catalog(models=_with_scores(_from_litellm()), source="litellm", fetched_at=0.0)
    _write_cache(fresh)
    return fresh


def _fetch() -> Catalog:
    response = httpx.get(_OPENROUTER_URL, timeout=_FETCH_TIMEOUT)
    response.raise_for_status()
    models = _from_openrouter(response.json())
    if not models:
        raise RefreshFailed("OpenRouter returned nothing usable")
    return Catalog(models=_with_scores(models), source="openrouter", fetched_at=time.time())
