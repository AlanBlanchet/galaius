#!/usr/bin/env python3
"""Generate the bundled models.json for the VS Code extension.

models.json carries TWO things:
1. A full registry mirror of every vision-capable model litellm knows about —
   never filter the registry; downstream code queries it by capability.
2. A curated `recommendations` / `defaults` set built by `_compute_recommendations`
   that drops models below the 25th-percentile intelligence score (unless they
   have a published GUI-grounding score), so users are steered toward current
   high-quality models rather than deprecated generations.
"""

import json
import math
import os
import re
import signal
from contextlib import contextmanager
from pathlib import Path

from interact.data import PackageData
from interact.dotenv_loader import load_dotenv_for_cli
from interact.models import is_audio_model, is_native_video_model

load_dotenv_for_cli()

import litellm

_ENV_PATTERNS = re.compile(
    r"_(API_KEY|API_BASE|API_SECRET|ACCESS_KEY|SECRET_KEY|API_VERSION)$"
)

FAKE_PROVIDER_RE = re.compile(r"^\d+[-_x]+\d+$|^v\d+$")
FAKE_PROVIDER_NAMES = {"high", "low", "medium", "standard"}

# Providers where litellm.validate_environment() misses required keys.
# These use get_secret_str() in their transformation class instead.
_EXTRA_ENV_KEYS: dict[str, list[str]] = {
    "ollama": ["OLLAMA_API_KEY"],  # extension auto-sets OLLAMA_API_BASE for cloud
    "zai": ["ZAI_API_KEY"],
}

# Keys that litellm reports but are auto-provided (not user secrets).
# These are excluded from the envKeys list in models.json.
_AUTO_PROVIDED_KEYS: set[str] = {
    "OLLAMA_API_BASE",  # extension auto-sets to https://api.ollama.com when OLLAMA_API_KEY present
}

# Keys that are aliases — litellm accepts either, so only store the canonical one.
_KEY_ALIASES: dict[str, str] = {
    "GOOGLE_API_KEY": "GEMINI_API_KEY",
}

# Task descriptions shown in the extension QuickPick
_TASK_DESCRIPTIONS: dict[str, str] = {
    "component": "UI element detection (bounding boxes)",
    "image": "Screenshot/image analysis",
    "video": "Video analysis",
    "audio": "Audio transcription + understanding",
}

# A modality task only recommends models that actually have the capability (sourced from the
# curated family tables in interact.models, since litellm's supports_video_input/_audio_input
# flags don't populate). Other tasks (image/component) consider every vision model.
_TASK_REQUIRED_CAP: dict[str, str] = {"video": "video", "audio": "audio"}

# Transcription-only models (no chat audio) aren't vision models, so they're absent from the
# catalog candidate pool — appended as the audio chain's tail so auto-audio can still transcribe
# when no audio-chat model (Gemini) is keyed.
_AUDIO_TAIL: list[str] = ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"]

# Coordinate format by model family — a per-MODEL fact, keyed by model prefix and
# carried with the model (into models.json `coordFormats`). It describes the model's
# OWN grounding output space, taken from each model's docs — NOT the screen/window:
#   - `normalized` + `divisor` (default 1000): the model emits 0..divisor coords
#     (e.g. Gemini 0-1000); we scale by img_w/divisor. Absolute-pixel models (Qwen)
#     set neither, so the divisor never applies (see CoordFormat.parse / .meta).
#   - `box_order` / `box_key`: how the model orders/keys its box (yxyx vs xyxy, etc).
# Inferring this from output is unreliable (0-1000 looks like pixels), so it's a
# curated table — refresh from the model's grounding docs when a model changes.
_COORD_FORMATS: dict[str, dict] = {
    "gemini/": {
        "normalized": True,
        "box_order": "yxyx",
        "box_key": "box_2d",
        "prompt_template": (
            "Return as JSON array. For each element provide bounding box coordinates "
            "in [ymin, xmin, ymax, xmax] format where values range from 0 to 1000. "
            '[{{"role":"button","name":"OK","box_2d":[200,100,260,180]}}]'
        ),
    },
    "zai/": {"normalized": True, "box_order": "xyxy"},
    "ollama/qwen": {"box_order": "xyxy", "box_key": "bbox_2d"},
    "openai/": {"box_order": "xyxy", "box_key": "bbox"},
}


# Regional/deployment prefixes that indicate a wrapper around a base model.
# These are valid models but shouldn't dominate recommendations over the
# direct-API version of the same model.
_REGIONAL_PREFIX_RE = re.compile(
    r"^(au|eu|jp|us|us-gov|ap|ca|sa|apac|global)\."
    r"|(^azure/)"
    r"|(^azure_ai/)"
    r"|(^vertex_ai/)"
    r"|(^bedrock/)"
    r"|(^sagemaker/)"
    r"|(^openrouter/)"
    r"|(^github_copilot/)"
    r"|(^gmi/)"
)

# Providers that are wrappers/proxies around another provider's models.
# Direct-API providers (anthropic, openai, gemini, etc.) are preferred
# in recommendations since most users access them directly.
_WRAPPER_PROVIDERS = frozenset(
    {
        "azure",
        "azure_ai",
        "bedrock",
        "bedrock_converse",
        "vertex_ai",
        "vertex_ai-anthropic_models",
        "vertex_ai-language-models",
        "vertex_ai-llama_models",
        "vertex_ai-mistral_models",
        "openrouter",
        "github_copilot",
        "gmi",
        "sagemaker",
        "vercel_ai_gateway",
        "llamagate",
    }
)


@contextmanager
def _cleared_api_env():
    saved = {k: os.environ.pop(k) for k in list(os.environ) if _ENV_PATTERNS.search(k)}
    try:
        yield
    finally:
        os.environ.update(saved)


def _get_env_keys(models: list[str]) -> list[str]:
    for model in models:
        signal.alarm(3)
        try:
            with _cleared_api_env():
                info = litellm.validate_environment(model)
            keys = info.get("missing_keys", [])
            if keys:
                # Deduplicate alias keys — keep only the canonical name
                canonical = [_KEY_ALIASES.get(k, k) for k in keys]
                # Exclude auto-provided keys (set by extension, not user secrets)
                return sorted(set(canonical) - _AUTO_PROVIDED_KEYS)
        except Exception:
            continue
        finally:
            signal.alarm(0)
    return []


# Regex matching clean size tags (e.g. "4b", "12b", "27b-cloud", "cloud") but not
# quantization variants (e.g. "12b-it-q4_K_M", "1b-it-fp16").
_OLLAMA_TAG_RE = re.compile(r"^(\d+[bm](-\w+)?-cloud|cloud|\d+[bm]|latest)$")


def _fetch_ollama_models() -> dict[str, dict]:
    """Fetch vision-capable models from the Ollama registry (source of truth).

    Returns model entries keyed by ``ollama/{name}:{tag}`` or ``ollama/{name}``.
    Each entry has ``cloud`` (bool) and ``vision`` (bool) flags.
    """
    import urllib.request

    headers = {"User-Agent": "interact/1.0"}

    # 1. Get vision model base names from the search page
    try:
        req = urllib.request.Request(
            "https://ollama.com/search?c=vision", headers=headers
        )
        html = urllib.request.urlopen(req, timeout=15).read().decode()
    except Exception as e:
        print(f"Warning: Failed to fetch Ollama vision models: {e}")
        return {}

    bases = sorted(
        {
            m.group(1)
            for m in re.finditer(r'href="/library/([^"/?]+)"', html)
            if m.group(1)
        }
    )
    if not bases:
        print("Warning: No vision models found on ollama.com")
        return {}

    # 2. For each base model, fetch tag variants
    models: dict[str, dict] = {}
    for base in bases:
        try:
            req = urllib.request.Request(
                f"https://ollama.com/library/{base}/tags", headers=headers
            )
            tag_html = urllib.request.urlopen(req, timeout=10).read().decode()
        except Exception:
            # Fallback: just add base name with no specific tag
            models[f"ollama/{base}"] = {
                "supports_response_schema": False,
                "vision": True,
            }
            continue

        tags = sorted(
            {
                t.group(1)
                for t in re.finditer(
                    rf'href="/library/{re.escape(base)}:([^"]+)"', tag_html
                )
                if _OLLAMA_TAG_RE.match(t.group(1))
            }
        )
        if not tags:
            models[f"ollama/{base}"] = {
                "supports_response_schema": False,
                "vision": True,
            }
        else:
            local_tags = [t for t in tags if "cloud" not in t]
            cloud_only = not local_tags  # e.g. kimi-k2.5 has only :cloud
            for tag in tags:
                is_cloud = "cloud" in tag
                key = f"ollama/{base}:{tag}" if tag != "latest" else f"ollama/{base}"
                models[key] = {
                    "supports_response_schema": False,
                    "vision": True,
                    **(
                        {"cloud": True}
                        if is_cloud or (tag == "latest" and cloud_only)
                        else {}
                    ),
                }

    print(
        f"Fetched {len(models)} Ollama vision models from ollama.com ({len(bases)} families)"
    )
    return models


def _fetch_aa_scores() -> dict[str, float]:
    """Fetch Artificial Analysis intelligence scores. Returns slug→score mapping."""
    import urllib.request

    api_key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        req = urllib.request.Request(
            "https://artificialanalysis.ai/api/v2/data/llms/models",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"Warning: Failed to fetch AA scores: {e}")
        return {}

    scores: dict[str, float] = {}
    for item in (
        data if isinstance(data, list) else data.get("data", data.get("models", []))
    ):
        slug = item.get("slug", "") or item.get("name", "")
        evals = item.get("evaluations", {})
        score = evals.get("artificial_analysis_intelligence_index")
        if slug and score is not None:
            scores[slug.lower()] = float(score)
    return scores


def _normalize_model_slug(name: str) -> str:
    """Normalize a model name for fuzzy comparison."""
    # Strip provider prefix, lowercase, unify separators/versions
    s = name.split("/")[-1].lower()
    # Remove date suffixes BEFORE version normalization to avoid mangling
    s = re.sub(r"-\d{4,8}(-\d{2}){0,2}$", "", s)
    # Remove Bedrock version suffixes: -v1:0
    s = re.sub(r"-v\d+:\d+$", "", s)
    # Normalize version separators: 2-5 → 2.5, 3_5 → 3.5
    s = re.sub(r"(\d+)[-_](\d+)", r"\1.\2", s)
    # Drop remaining dashes/underscores/spaces for substring comparison
    return s.replace("-", "").replace("_", "").replace(" ", "")


def _sorted_tokens(name: str) -> str:
    """Sort alphanumeric tokens for word-order-invariant matching.

    claude-sonnet-4-5 and claude-4-5-sonnet both become '4.5 claude sonnet'.
    """
    s = name.split("/")[-1].lower()
    s = re.sub(r"-\d{4,8}(-\d{2}){0,2}$", "", s)
    s = re.sub(r"-v\d+:\d+$", "", s)
    s = re.sub(r"(\d+)[-_](\d+)", r"\1.\2", s)
    # Remove noise words
    for noise in ("latest", "preview", "customtools"):
        s = s.replace(noise, "")
    tokens = sorted(t for t in re.split(r"[-_\s]+", s) if t)
    return " ".join(tokens)


def _match_aa_score(model: str, aa_scores: dict[str, float]) -> float | None:
    """Fuzzy-match a litellm model name to AA scores."""
    if not aa_scores:
        return None
    bare = model.split("/")[-1].lower()
    # Exact match first
    if bare in aa_scores:
        return aa_scores[bare]
    # Normalized exact match
    norm_bare = _normalize_model_slug(model)
    for key, score in aa_scores.items():
        if _normalize_model_slug(key) == norm_bare:
            return score
    # Sorted-token match (word-order invariant: claude-sonnet-4-5 ↔ claude-4-5-sonnet)
    sorted_bare = _sorted_tokens(model)
    for key, score in aa_scores.items():
        if _sorted_tokens(key) == sorted_bare:
            return score
    # Substring match — prefer longest matching key
    best_key, best_len = None, 0
    for key in aa_scores:
        norm_key = _normalize_model_slug(key)
        if norm_key in norm_bare or norm_bare in norm_key:
            if len(norm_key) > best_len:
                best_key, best_len = key, len(norm_key)
    return aa_scores[best_key] if best_key else None


def _model_quality_score(info: dict, aa_score: float | None) -> float:
    """Heuristic quality score from litellm metadata when no AA data."""
    if aa_score is not None:
        return aa_score
    # Derive from max_output_tokens and input_cost as proxy for capability
    # Log scale on cost breaks perfect correlation with cost_norm for normalization
    max_out = info.get("max_output_tokens", 0) or 0
    input_cost = info.get("input_cost_per_token", 0) or 0
    return min(max_out / 1000, 50) + math.log1p(input_cost * 1e6) * 10


def _canonical_model_name(name: str) -> str:
    """Strip regional/deployment prefixes to get the underlying model identity."""
    # Strip provider-path prefixes: azure/gpt-4o → gpt-4o
    s = name.split("/")[-1].lower()
    # Strip regional dot prefixes: au.anthropic.claude-x → anthropic.claude-x
    s = _REGIONAL_PREFIX_RE.sub("", s)
    # Strip Bedrock provider prefixes: anthropic.claude-sonnet → claude-sonnet
    s = re.sub(r"^(anthropic|meta|cohere|mistral|amazon|ai21|stability)\.", "", s)
    # Strip noise suffixes that don't change the model identity
    for noise in ("customtools", "search-preview", "preview"):
        s = s.removesuffix(f"-{noise}")
    # Strip version/date suffixes for family grouping: claude-sonnet-4-20250514 → claude-sonnet-4
    s = re.sub(r"-\d{4,8}(-\d{2}){0,2}(-v\d+:\d+)?$", "", s)
    s = re.sub(r"-v\d+:\d+$", "", s)
    return s


def _compute_recommendations(
    provider_data: dict,
    aa_scores: dict[str, float],
) -> dict[str, list[str]]:
    """Compute top model recommendations per task with min-max normalized scoring."""
    from interact.benchmarks.published import PublishedTable  # noqa: PLC0415

    # Collect raw dimensions for all models
    raw: list[tuple[str, float, float, bool, dict]] = []
    for pinfo in provider_data.values():
        for model_name, model_meta in pinfo["models"].items():
            aa = model_meta.get("intelligence_score")
            intelligence = aa if aa is not None else model_meta.get("_quality_score", 0)
            cost = model_meta.get("input_cost_per_million", 0) or 0
            has_structured = model_meta.get("supports_response_schema", False)
            raw.append((model_name, intelligence, cost, has_structured, model_meta))

    # Intelligence p25 threshold over real AA-scored models only (None excluded)
    real_scores = sorted(
        r[4].get("intelligence_score")
        for r in raw
        if r[4].get("intelligence_score") is not None
    )
    if real_scores:
        idx = max(0, int(len(real_scores) * 0.25) - 1)
        intel_threshold = real_scores[idx]
    else:
        intel_threshold = 0.0
    print(f"Intelligence threshold (p25): {intel_threshold:.1f}")

    # Min-max ranges
    scores = [r[1] for r in raw]
    costs = [r[2] for r in raw]
    min_score, max_score = min(scores), max(scores)
    min_cost, max_cost = min(costs), max(costs)
    score_range = max_score - min_score or 1.0
    cost_range = max_cost - min_cost or 1.0

    # Task weights: {task: (structured_w, intelligence_w, cost_w)}
    task_weights = {
        "component": (0.35, 0.15, 0.50),
        "image": (0.15, 0.6, 0.25),
        "video": (0.15, 0.6, 0.25),
        "audio": (0.15, 0.6, 0.25),
    }

    recommendations: dict[str, list[str]] = {}
    for task in _TASK_DESCRIPTIONS:
        w_struct, w_intel, w_cost = task_weights.get(task, (0.15, 0.6, 0.25))
        # A modality task ranks ONLY models with its capability (video/audio); image/component
        # rank every vision model. This is what stops the video list mirroring the image list.
        cap_required = _TASK_REQUIRED_CAP.get(task)
        candidates = (
            [r for r in raw if cap_required in (r[4].get("capabilities") or [])]
            if cap_required
            else raw
        )

        scored: list[tuple[float, str]] = []
        for model_name, intelligence, cost, has_structured, model_meta in candidates:
            intel_norm = (intelligence - min_score) / score_range
            cost_norm = 1.0 - (cost - min_cost) / cost_range
            struct_norm = 1.0 if has_structured else 0.0

            flag_bonus = 0.0

            # Penalize wrapper/regional providers — prefer direct API
            llm_provider = model_meta.get("_litellm_provider", "")
            if llm_provider in _WRAPPER_PROVIDERS or _REGIONAL_PREFIX_RE.search(
                model_name
            ):
                flag_bonus -= 0.15

            # Penalize models without real benchmark data — unverified quality
            if model_meta.get("intelligence_score") is None:
                flag_bonus -= 0.25

            final = (
                struct_norm * w_struct
                + intel_norm * w_intel
                + cost_norm * w_cost
                + flag_bonus
            )
            scored.append((final, model_name))
        scored.sort(key=lambda t: t[0], reverse=True)

        # Drop models that fail the data-driven quality gate:
        #   - intelligence_score is None AND no published GUI-grounding score, OR
        #   - intelligence_score is below the p25 threshold.
        # Models without AA but with a published grounding score survive.
        filtered: list[tuple[float, str]] = []
        for score, name in scored:
            intel = next(
                (r[4].get("intelligence_score") for r in raw if r[0] == name), None
            )
            if intel is None:
                if PublishedTable.score_for_model(name) is None:
                    continue
            elif intel < intel_threshold:
                continue
            filtered.append((score, name))
        scored = filtered

        # Deduplicate model families — keep only the first (highest-scoring)
        # variant per canonical model name
        seen_canonical: set[str] = set()
        deduped: list[tuple[float, str]] = []
        for score, name in scored:
            canon = _canonical_model_name(name)
            if canon not in seen_canonical:
                seen_canonical.add(canon)
                deduped.append((score, name))
        recommendations[task] = [name for _, name in deduped[:10]]

        # Print top-5 for verification
        print(f"\nTop-5 {task}:")
        for rank, (score, name) in enumerate(deduped[:5], 1):
            print(f"  {rank}. {name} ({score:.3f})")

    # Transcription-only models aren't vision models (absent from the candidate pool) — append
    # them so auto-audio can still transcribe when no audio-chat model (Gemini) is available.
    for mid in _AUDIO_TAIL:
        if mid not in recommendations.setdefault("audio", []):
            recommendations["audio"].append(mid)

    return recommendations


signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))

aa_scores = _fetch_aa_scores()
if aa_scores:
    print(f"Fetched {len(aa_scores)} Artificial Analysis scores")
else:
    print("Warning: No AA scores available — falling back to cost-based heuristic")

providers: dict[str, dict] = {}
for model, info in litellm.model_cost.items():
    if not info.get("supports_vision"):
        continue
    provider = info.get("litellm_provider", "")
    if (
        not provider
        or " " in provider
        or provider in FAKE_PROVIDER_NAMES
        or FAKE_PROVIDER_RE.match(provider)
    ):
        continue
    if provider not in providers:
        env_keys = _get_env_keys([model])
        # Merge extra keys that validate_environment() misses (e.g. OLLAMA_API_KEY)
        for extra in _EXTRA_ENV_KEYS.get(provider, []):
            if extra not in env_keys:
                env_keys.append(extra)
        providers[provider] = {"envKeys": env_keys, "models": {}}

    aa_score = _match_aa_score(model, aa_scores)
    input_cost = info.get("input_cost_per_token")
    output_cost = info.get("output_cost_per_token")

    model_entry: dict = {
        "supports_response_schema": info.get("supports_response_schema", False),
    }
    if input_cost is not None:
        model_entry["input_cost_per_million"] = round(input_cost * 1e6, 4)
    if output_cost is not None:
        model_entry["output_cost_per_million"] = round(output_cost * 1e6, 4)
    if aa_score is not None:
        model_entry["intelligence_score"] = round(aa_score, 2)

    # Capabilities — derived from litellm's live flags (soft-link, not a hardcoded list) plus
    # the coord-format prefix table for grounding box conventions.
    caps = ["vlm"]
    model_lower = model.lower()
    for prefix in _COORD_FORMATS:
        if model_lower.startswith(prefix):
            caps.append("gui_grounding")
            break
    if info.get("supports_computer_use"):  # native click-coordinate output (Anthropic/OpenAI CU)
        caps.append("computer_use")
    # video/audio: litellm's supports_*_input flags are unreliable (return nothing), so OR them
    # with the curated family tables in interact.models (Gemini/Qwen-VL/Nova = video; Gemini/
    # gpt-4o-audio/Whisper/Qwen-Omni = audio).
    if info.get("supports_video_input") or is_native_video_model(model):
        caps.append("video")
    if info.get("supports_audio_input") or is_audio_model(model):
        caps.append("audio")
    model_entry["capabilities"] = caps

    # Internal score for recommendation computation (not written to JSON)
    model_entry["_quality_score"] = _model_quality_score(info, aa_score)
    model_entry["_litellm_provider"] = provider

    providers[provider]["models"][model] = model_entry

# Merge Ollama vision models from online registry (source of truth).
# These supplement litellm's stale/incomplete ollama list.
ollama_models = _fetch_ollama_models()
if ollama_models:
    if "ollama" not in providers:
        env_keys = []
        for extra in _EXTRA_ENV_KEYS.get("ollama", []):
            if extra not in env_keys:
                env_keys.append(extra)
        providers["ollama"] = {"envKeys": env_keys, "models": {}}
    for model_name, entry in ollama_models.items():
        if model_name in providers["ollama"]["models"]:
            continue  # litellm entry takes precedence (has cost data)
        aa_score = _match_aa_score(model_name, aa_scores)
        if aa_score is not None:
            entry["intelligence_score"] = round(aa_score, 2)
        entry["_quality_score"] = _model_quality_score({}, aa_score)
        entry["_litellm_provider"] = "ollama"
        providers["ollama"]["models"][model_name] = entry

# Count AA matches
aa_matched = sum(
    1
    for pinfo in providers.values()
    for m in pinfo["models"].values()
    if m.get("intelligence_score") is not None
)
aa_total = sum(len(pinfo["models"]) for pinfo in providers.values())
print(f"\nAA score matched: {aa_matched}/{aa_total} models")

# Compute recommendations
recommendations = _compute_recommendations(providers, aa_scores)

# Append per-benchmark best published model (lookup-only — no eval runs).
# Keys: "screenspot", "screenspot_pro" — values: list[model_id] for shape parity.
from interact.benchmarks.published import PublishedTable  # noqa: E402
from interact.models import Model  # noqa: E402

Model.load_registry()
for _bid in ("screenspot", "screenspot_pro"):
    _best = PublishedTable.best(_bid, available_only=False)
    if _best is not None:
        recommendations[_bid] = [_best[0]]

# Add gui_grounding to component-recommended models
for model_name in recommendations.get("component", []):
    for pinfo in providers.values():
        if model_name in pinfo["models"]:
            caps = pinfo["models"][model_name].get("capabilities", ["vlm"])
            if "gui_grounding" not in caps:
                caps.append("gui_grounding")
            pinfo["models"][model_name]["capabilities"] = caps

# Strip internal fields before writing
for pinfo in providers.values():
    for model_meta in pinfo["models"].values():
        model_meta.pop("_quality_score", None)
        model_meta.pop("_litellm_provider", None)

# Sort models within each provider
for pinfo in providers.values():
    pinfo["models"] = dict(sorted(pinfo["models"].items()))

# Build defaults programmatically:
#   - component.model: top-ranked recommendation with a verified coord format
#     entry in _COORD_FORMATS (native grounding support, not just tagged).
#   - image.model / video.model: top AA-ranked picks from recommendations.

_component_default: str | None = None
# Prefer the top-ranked recommendation whose model prefix has a _COORD_FORMATS entry.
# This ensures we pick models with VERIFIED bounding-box formats (Gemini, Qwen, ZAI).
for _candidate in recommendations.get("component", []):
    _cl = _candidate.lower()
    if any(_cl.startswith(prefix) for prefix in _COORD_FORMATS):
        _component_default = _candidate
        print(f"\n[defaults] component.model ← {_candidate}  (verified coord format)")
        break
if _component_default is None:
    _component_default = (recommendations.get("component") or [""])[0]

defaults = {
    "component.model": _component_default,
    "image.model": (recommendations.get("image") or [""])[0],
    "video.model": (recommendations.get("video") or [""])[0],
    "audio.model": (recommendations.get("audio") or [""])[0],
}

key_aliases_bidir = {}
for k, v in _KEY_ALIASES.items():
    key_aliases_bidir[k] = v
    key_aliases_bidir[v] = k

coord_formats = {prefix: fmt for prefix, fmt in _COORD_FORMATS.items()}

result = {
    "providers": dict(sorted(providers.items())),
    "recommendations": recommendations,
    "taskDescriptions": _TASK_DESCRIPTIONS,
    "defaults": defaults,
    "keyAliases": key_aliases_bidir,
    "coordFormats": coord_formats,
}

print("\nDefaults:")
for setting, model in defaults.items():
    print(f"  {setting}: {model}")

out = PackageData.path(PackageData.MODELS)
out.write_text(json.dumps(result, indent=2) + "\n")
total = sum(len(p["models"]) for p in result["providers"].values())
print(f"Wrote {total} models across {len(result['providers'])} providers to {out}")
