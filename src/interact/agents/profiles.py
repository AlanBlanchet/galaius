"""Per-agent model routing — deciding what an agent runs on BEFORE it is called.

Alan: "why couldn't we mix? Can't we control the env that an agent (or sub-agent) spawns in by
activating / deactivating what the agent can do before called? Even for other providers?"

Yes, and his framing is the safe one. The tempting shape is an ``env`` dict on the spawn tool:
four lines, total flexibility. It is also a model-reachable path to ``LD_PRELOAD``, ``PATH`` and
key exfiltration — the same escalation shape this codebase already refuses once, where a model may
not select an unrestricted permission mode for the agent it spawns. A capability a model can grant
itself is not a capability the operator controls.

So the operator names a PROFILE in their own config, and a profile resolves to a fixed,
allow-listed set of variables. There is no input to this module that produces a key outside
``ALLOWED_ENV`` — not a crafted model id, not a newline, not a profile value written to look like
an assignment. Flexibility lives in WHICH profiles exist; the blast radius does not move.

    # ~/.interact/config.env
    INTERACT_PROFILE_CHEAP=ollama/deepseek-v4-flash
    INTERACT_PROFILE_SHARP=anthropic/claude-opus-4-6
    INTERACT_PROFILE_LOCAL=vllm/glm-5.2          # self-hosted box, OpenAI wire protocol
    VLLM_BASE_URL=http://gpubox.lan:8000/v1
    INTERACT_PROFILE_ROUTED=hf/glm-5.2           # Hugging Face's router (default base URL)

Two DISTINCT wire protocols exist, and each provider name here declares which one it speaks
(``_WIRE_PROTOCOL``): ``ollama`` serves an Anthropic-compatible ``/v1/messages``, which is why an
unmodified Claude Code runs against it untouched — no argv changes, just ``ANTHROPIC_BASE_URL``.
``hf`` (Hugging Face's router, ``https://router.huggingface.co/v1``) and ``vllm`` (any self-hosted
OpenAI-wire server — vLLM, TGI, or Ollama's own ``/v1/chat/completions`` surface) speak OpenAI's
chat-completions protocol instead, which Claude Code cannot; those route through Codex (OpenAI's
own CLI, already fluent in that wire format) as a NAMED, isolated ``model_providers`` entry —
never the built-in ``openai``/``chatgpt`` provider ids, which Codex ties to the user's own ChatGPT
session or real OpenAI key. A self-hosted box or HF's router never sees that credential; it sees
only whatever token the operator names for THAT endpoint, via ``_KEY_FROM``.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from interact.agents.vocabulary import ModelSource

PROFILE_PREFIX = "INTERACT_PROFILE_"

#: The ONLY variables a profile may set. Deliberately tiny and deliberately boring: every entry
#: selects a model or an endpoint, and none of them can load code, change what binary runs, or
#: redirect an import. Adding to this list is a security decision, not a convenience one. The
#: OPENAI_* trio mirrors the ANTHROPIC_* one for the second wire protocol this module now routes:
#: a base URL, the bare model name, and an (optional) bearer credential — never the real
#: ``OPENAI_API_KEY`` name a native OpenAI/ChatGPT login might already be using in the same
#: process (see ``_KEY_FROM``).
ALLOWED_ENV = frozenset({
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "INTERACT_OPENAI_COMPAT_KEY",
})

#: Where each provider's endpoint is read FROM — the operator's own environment, never the caller's
#: model string. A model id says WHICH model; it never says where to send the credentials. Adding
#: a row here is a routing decision, not a convenience one: it is the whole allow-list of
#: (provider name) -> (env var interact will actually look at) pairs. A provider token an attacker
#: could put in a model id (``[a-z0-9_-]{1,32}``, see ``_MODEL``) only ever selects a ROW already
#: written here at review time — it can never manufacture a new env var name to read.
_BASE_FROM: dict[str, tuple[str, ...]] = {
    "ollama": ("OLLAMA_API_BASE", "OLLAMA_HOST"),
    "hf": ("HF_BASE_URL",),
    "vllm": ("VLLM_BASE_URL",),
}

#: Default base URL when the operator sets no override — only for an endpoint with ONE obvious
#: address. Hugging Face's router has exactly one. A self-hosted box does not: guessing one would
#: be the same mistake this module's docstring already refuses ("guessing an endpoint would send
#: the operator's credentials somewhere nobody chose"), applied to a LAN address instead of a
#: vendor's.
_DEFAULT_BASE: dict[str, str] = {
    "ollama": "http://localhost:11434",
    "hf": "https://router.huggingface.co/v1",
}

#: Which wire protocol a provider name speaks, and therefore which output keys ``overlay_for``
#: writes. One row of typed config per PROTOCOL, not per provider — ``hf`` and ``vllm`` differ
#: only in WHERE their base URL/key come from, never in what a caller downstream does with them.
_PROTOCOL_ENV: dict[str, tuple[str, str]] = {
    "anthropic": ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"),
    "openai": ("OPENAI_BASE_URL", "OPENAI_MODEL"),
}
_WIRE_PROTOCOL: dict[str, str] = {
    "ollama": "anthropic",
    "hf": "openai",
    "vllm": "openai",
}

#: Operator-side env var carrying the BEARER TOKEN for an openai-protocol endpoint, keyed by
#: provider name — never the real ``OPENAI_API_KEY``/``HF_TOKEN`` name verbatim as the OUTPUT key
#: (that would collide with, or masquerade as, a native OpenAI/ChatGPT/huggingface_hub credential
#: already meaningful elsewhere in the same process). The value is copied into the single fixed
#: ``INTERACT_OPENAI_COMPAT_KEY`` overlay key instead, which only this module's own routed
#: ``model_providers`` entry ever reads (see ``agents/providers.py::CodexProvider``). Absent here,
#: or unset in the environment, means no Authorization header — correct for a bare self-hosted
#: server started with no ``--api-key``.
_KEY_FROM: dict[str, str] = {
    "ollama": "OLLAMA_API_KEY",
    "hf": "HF_TOKEN",
    "vllm": "VLLM_API_KEY",
}

#: A model id we will act on: provider, slash, then a plain name. Anything else routes nowhere.
_MODEL = re.compile(r"^([a-z0-9_-]{1,32})/([A-Za-z0-9._:-]{1,64})$")


def profiles_from(env: dict[str, str]) -> dict[str, str]:
    """The profiles the OPERATOR has defined, lower-cased by name."""
    return {
        k[len(PROFILE_PREFIX):].lower(): v.strip()
        for k, v in env.items()
        if k.startswith(PROFILE_PREFIX) and v.strip()
    }


def _base_url(provider: str, env: dict[str, str]) -> str | None:
    for key in _BASE_FROM.get(provider, ()):
        raw = (env.get(key) or "").strip()
        if not raw:
            continue
        # Ollama's own convention is scheme-less ("box.lan:11434"), so add the scheme it means —
        # but never past a scheme that ISN'T http(s), nor a value with no real host (a bare path
        # like "/etc/passwd" becomes "http:///etc/passwd": syntactically http, but no host to
        # connect to) or a garbled one (a `javascript:x` value fed through the same "no scheme ->
        # add http://" path lands as host "javascript" port "x", which fails port parsing).
        url = raw if "://" in raw else f"http://{raw}"
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        try:
            parsed.port
        except ValueError:
            continue
        return url
    return _DEFAULT_BASE.get(provider)


def overlay_for(model: str, env: dict[str, str]) -> dict[str, str]:
    """The environment overlay that points a vendor CLI at ``model``.

    Returns an EMPTY overlay for anything it does not positively recognise — an unprefixed model
    (the vendor's own default, nothing to redirect), a malformed id, a provider with no known
    endpoint, or a base URL that resolved to a non-http(s) scheme. Refusing to act is always safe
    here; guessing an endpoint would send the operator's credentials somewhere nobody chose.

    Two shapes come out, chosen by which wire protocol the provider speaks (``_WIRE_PROTOCOL``):
    Anthropic-shaped (``ANTHROPIC_BASE_URL``/``ANTHROPIC_MODEL``, what Claude Code reads natively)
    for ``ollama``; OpenAI-shaped (``OPENAI_BASE_URL``/``OPENAI_MODEL``, what a caller threads into
    Codex's own routed ``model_providers`` argv — see ``agents/providers.py``) for ``hf``/``vllm``.
    A caller distinguishes them by which KEY came back, never by truthiness alone — both shapes
    are equally non-empty.
    """
    match = _MODEL.match((model or "").strip())
    if not match:
        return {}
    provider, name = match.group(1), match.group(2)
    protocol = _WIRE_PROTOCOL.get(provider)
    if protocol is None:
        return {}
    base = _base_url(provider, env)
    if not base:
        return {}
    base_key, model_key = _PROTOCOL_ENV[protocol]
    out = {base_key: base, model_key: name}
    if protocol == "anthropic":
        # Claude Code warns `unrecognized_model` and assumes a 200k window for anything it does
        # not ship; saying so explicitly keeps a local model from being handed a context it
        # cannot hold. Meaningless to the openai protocol branch — Codex takes no such flag.
        window = (env.get("INTERACT_PROFILE_CONTEXT") or "").strip()
        if window.isdigit():
            out["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = window
        # Ollama cloud uses the Anthropic-compatible bearer header. Local Ollama has no auth;
        # never copy OLLAMA_API_KEY to a local endpoint by accident.
        if provider == "ollama":
            hostname = (urlsplit(base).hostname or "").lower()
            if hostname in {"ollama.com", "www.ollama.com"}:
                token = (env.get(_KEY_FROM[provider]) or "").strip()
                if token:
                    out["ANTHROPIC_AUTH_TOKEN"] = token
    else:
        token_key = _KEY_FROM.get(provider)
        token = (env.get(token_key) or "").strip() if token_key else ""
        if token:
            out["INTERACT_OPENAI_COMPAT_KEY"] = token
    assert set(out) <= ALLOWED_ENV  # structural, not defensive: the set is fixed above
    return out


def model_source_for(model: str, env: dict[str, str]) -> ModelSource | None:
    """Classify where a model is served without treating a vendor's spelling as an intent."""
    match = _MODEL.match((model or "").strip())
    if not match:
        return ModelSource.PROVIDER_LOGIN if model else None
    provider, name = match.group(1), match.group(2)
    if provider == "ollama":
        if name.endswith(":cloud"):
            return ModelSource.OLLAMA_CLOUD
        base = _base_url(provider, env) or ""
        return (
            ModelSource.OLLAMA_CLOUD
            if (urlsplit(base).hostname or "").lower() in {"ollama.com", "www.ollama.com"}
            else ModelSource.OLLAMA_LOCAL
        )
    if provider in {"hf", "vllm"}:
        return ModelSource.OPERATOR_ENDPOINT
    if provider == "cursor":
        return ModelSource.CURSOR_CLOUD
    return ModelSource.PROVIDER_LOGIN
