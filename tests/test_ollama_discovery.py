"""Live Ollama discovery — what the daemon actually has, not what a snapshot guessed.

The payloads below are VERBATIM shapes captured from a running daemon (ollama 0.22.0), because
the interesting cases are the ones a hand-invented fixture would never contain: a cloud model
reports ``remote_host`` and ``families: null``, an embedding model reports ``capabilities:
["embedding"]``, and a model that upstream RETIRED is still listed by ``/api/tags`` while
``/api/show`` answers with an error.
"""

import json

import httpx
import pytest

from galaius import ollama
from galaius.models import Model, ModelCapability

# --- captured from a real daemon -------------------------------------------------------------

TAGS = {
    "models": [
        {
            "name": "kimi-k3:cloud", "model": "kimi-k3:cloud",
            "remote_model": "kimi-k3", "remote_host": "https://ollama.com",
            "size": 308, "digest": "630e737485bd",
            "details": {"family": "", "families": None, "parameter_size": "2.81T"},
        },
        {
            "name": "bge-m3:latest", "model": "bge-m3:latest",
            "size": 1157672605, "digest": "7907646426",
            "details": {"family": "bert", "families": ["bert"], "parameter_size": "566.70M"},
        },
        {
            "name": "qwen3-coder-next:cloud", "model": "qwen3-coder-next:cloud",
            "remote_model": "qwen3-coder-next", "remote_host": "https://ollama.com:443",
            "size": 382, "digest": "aa626c11ae8d",
            "details": {"family": "qwen3next", "families": ["qwen3next"], "parameter_size": "80B"},
        },
    ]
}

SHOW = {
    "kimi-k3:cloud": {"capabilities": ["vision", "thinking", "completion", "tools"]},
    "bge-m3:latest": {"capabilities": ["embedding"]},
    # Upstream retired this one. The daemon still lists it; /api/show refuses.
    "qwen3-coder-next:cloud": {"error": "qwen3-coder-next was retired at 2026-07-15"},
}


def client(tags: dict | None = None, show: dict | None = None, seen: list | None = None):
    """An httpx client wired to an in-memory daemon — the real code path, no network."""
    tags, show = (TAGS if tags is None else tags), (SHOW if show is None else show)

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags)
        if request.url.path == "/api/show":
            name = json.loads(request.content)["model"]
            return httpx.Response(200, json=show.get(name, {"error": "not found"}))
        return httpx.Response(404)

    return httpx.Client(base_url=ollama.DEFAULT_BASE, transport=httpx.MockTransport(handle))


def failing_client(exc: Exception):
    def handle(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.Client(base_url=ollama.DEFAULT_BASE, transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def _clean_discovery_state(monkeypatch, tmp_path):
    """Make every test here hermetic: no real socket, no ambient Ollama env, no shared cache.

    `_port_open` is the subtle one. `discover_cached` TCP-probes a local base before building a
    client, so a test that patches `_open` alone still opens a real socket to localhost:11434 —
    which passes on a developer box with a daemon running and FAILS on CI, where nothing listens.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("OLLAMA_API_BASE", "OLLAMA_HOST", "OLLAMA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ollama, "_port_open", lambda *a, **k: True)
    ollama.reset_cache()
    yield
    ollama.reset_cache()


# --- the daemon read --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "vision", "cloud", "chat", "size"),
    [
        # The model he pulled and paid for: vision read off the daemon, not guessed from the name.
        ("kimi-k3:cloud", True, True, True, "2.81T"),
        # Local, and it answers vectors rather than prompts — never offerable as a VLM.
        ("bge-m3:latest", False, False, False, "566.70M"),
        # Retired upstream: still pulled, still listed, but we may not invent a capability for it.
        ("qwen3-coder-next:cloud", False, True, True, "80B"),
    ],
)
def test_each_kind_of_model_the_daemon_reports_is_read_correctly(name, vision, cloud, chat, size):
    found = {m.name: m for m in ollama.discover(client=client())}

    assert name in found
    model = found[name]
    assert model.model_id == f"ollama/{name}"
    assert (model.vision, model.cloud, model.chat, model.parameter_size) == (
        vision,
        cloud,
        chat,
        size,
    )


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("garbage"),
    ],
)
def test_an_unreachable_daemon_degrades_silently(exc):
    """A user with no Ollama must see no error and no traceback — just an empty list."""
    assert ollama.discover(client=failing_client(exc)) == []


def test_malformed_json_degrades_silently():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not ollama</html>")

    with httpx.Client(base_url=ollama.DEFAULT_BASE, transport=httpx.MockTransport(handle)) as c:
        assert ollama.discover(client=c) == []


def test_capabilities_already_known_are_not_re_probed():
    """/api/show costs a round trip to ollama.com for a cloud model; a known digest skips it."""
    seen: list[httpx.Request] = []
    known = {"630e737485bd": ["vision", "completion"]}
    found = {m.name: m for m in ollama.discover(client=client(seen=seen), known=known)}

    assert found["kimi-k3:cloud"].vision is True
    probed = [json.loads(r.content)["model"] for r in seen if r.url.path == "/api/show"]
    assert "kimi-k3:cloud" not in probed  # served from what we already knew
    assert "bge-m3:latest" in probed  # unknown digest still probed


@pytest.mark.parametrize(
    ("key", "base", "sent"),
    [
        # Ollama Cloud's own endpoint needs it, and TLS protects it in transit.
        ("sk-test-123", "https://ollama.com", True),
        # Loopback needs no key, but it cannot leave the machine either (and the daemon ignores
        # it — verified: a real daemon answers 200 to a bogus bearer).
        ("sk-test-123", "http://localhost:11434", True),
        # THE ONE THAT MATTERS: a paid credential must never cross a network in cleartext.
        # `OLLAMA_HOST=box.lan:11434` is Ollama's own documented, scheme-less convention, so this
        # is what a user pointing galaius at a shared workstation actually gets.
        ("sk-test-123", "http://box.lan:11434", False),
        ("", "https://ollama.com", False),
    ],
)
def test_the_key_travels_only_where_it_cannot_leak(monkeypatch, key, base, sent):
    monkeypatch.setenv("OLLAMA_API_KEY", key) if key else monkeypatch.delenv(
        "OLLAMA_API_KEY", raising=False
    )
    with ollama._open(base) as c:
        assert ("authorization" in c.headers) is sent


# --- where we look ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected_first"),
    [
        ({}, "http://localhost:11434"),
        ({"OLLAMA_HOST": "http://box.lan:11434"}, "http://box.lan:11434"),
        # Ollama's own convention: OLLAMA_HOST is often scheme-less.
        ({"OLLAMA_HOST": "box.lan:11434"}, "http://box.lan:11434"),
        ({"OLLAMA_HOST": "127.0.0.1:11434/"}, "http://127.0.0.1:11434"),
        ({"OLLAMA_API_BASE": "https://api.ollama.com"}, "https://api.ollama.com"),
    ],
)
def test_the_configured_host_is_honoured(monkeypatch, env, expected_first):
    for name in ("OLLAMA_HOST", "OLLAMA_API_BASE", "OLLAMA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert ollama.candidate_bases()[0] == expected_first


def test_ollama_cloud_is_the_fallback_when_a_key_is_set_but_no_daemon_runs(monkeypatch):
    """He paid for Ollama Cloud. With a key and no local daemon, the cloud endpoint still answers."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test-123")
    assert ollama.candidate_bases() == ["http://localhost:11434", "https://ollama.com"]


def test_no_key_means_no_wan_round_trip(monkeypatch):
    """A user with no Ollama at all must never pay a wide-area timeout for a feature they
    do not use — only the loopback probe, which refuses instantly."""
    for name in ("OLLAMA_HOST", "OLLAMA_API_BASE", "OLLAMA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert ollama.candidate_bases() == ["http://localhost:11434"]


def test_discovery_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("OLLAMA_DISCOVERY", "0")
    assert ollama.discover_cached() == []


def test_the_process_memo_serves_a_second_call_without_a_second_round_trip(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(ollama, "_open", lambda base: client(seen=seen))
    monkeypatch.setenv("OLLAMA_DISCOVERY", "1")

    first = ollama.discover_cached()
    calls = len(seen)
    second = ollama.discover_cached()

    assert [m.name for m in first] == [m.name for m in second]
    assert len(seen) == calls  # nothing went out the second time


def test_capabilities_survive_into_a_fresh_process_via_the_disk_cache(monkeypatch):
    """The expensive half (/api/show) is cached by DIGEST, so a re-pull re-probes but a restart
    does not."""
    seen: list[httpx.Request] = []
    # A fresh client per call: discover_cached closes the one it opens, as a real caller would.
    monkeypatch.setattr(ollama, "_open", lambda base: client(seen=seen))
    monkeypatch.setenv("OLLAMA_DISCOVERY", "1")

    ollama.discover_cached()
    ollama._MEMO.clear()  # a fresh process: no memo, but the disk cache stands
    seen.clear()
    again = ollama.discover_cached()

    assert {m.name for m in again} == {row["name"] for row in TAGS["models"]}
    # Including the retired one: a failure is remembered too, so a permanently-broken model does
    # not cost a wide-area round trip in every process that loads the registry.
    reprobed = [json.loads(r.content)["model"] for r in seen if r.url.path == "/api/show"]
    assert reprobed == []


def test_a_failed_capability_read_is_retried_once_it_ages_out(monkeypatch):
    """A failure must not be believed forever — upstream fixes things, and a model that was
    briefly unreachable has to get another chance."""
    seen: list[httpx.Request] = []
    monkeypatch.setattr(ollama, "_open", lambda base: client(seen=seen))
    monkeypatch.setenv("OLLAMA_DISCOVERY", "1")
    ollama.discover_cached()

    stored = ollama._CAPABILITY_CACHE.read()
    assert "aa626c11ae8d" in stored["failures"], "the retired model's failure should be recorded"
    stored["failures"]["aa626c11ae8d"] -= ollama._FAILURE_TTL + 1
    ollama._CAPABILITY_CACHE.write(stored)
    ollama._MEMO.clear()
    seen.clear()

    ollama.discover_cached()
    reprobed = [json.loads(r.content)["model"] for r in seen if r.url.path == "/api/show"]
    assert reprobed == ["qwen3-coder-next:cloud"]


# --- the registry merge -----------------------------------------------------------------------


@pytest.fixture
def discovered(monkeypatch):
    """Load the registry with the daemon answering, without touching the network."""
    monkeypatch.setattr(ollama, "discover_cached", lambda: ollama.discover(client=client()))
    Model.load_registry()
    yield
    monkeypatch.undo()
    Model.load_registry()


def test_a_pulled_model_joins_the_registry(discovered):
    model = Model.by_id("ollama/kimi-k3:cloud")
    assert model is not None
    assert model.provider == "ollama"
    assert model.can(ModelCapability.VLM)


def test_an_embedding_model_never_enters_the_registry(discovered):
    """It cannot answer a prompt, so it must not be a fallback candidate for anything."""
    assert Model.by_id("ollama/bge-m3:latest") is None


def test_a_discovered_model_needs_no_api_key_to_be_available(monkeypatch):
    """The daemon ANSWERED. That is the availability proof — a local Ollama needs no key, and
    demanding OLLAMA_API_KEY is exactly what made a pulled model invisible."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(ollama, "discover_cached", lambda: ollama.discover(client=client()))
    Model.load_registry()
    try:
        model = Model.by_id("ollama/kimi-k3:cloud")
        assert model.is_available() is True
        assert model.key_missing() is False
        assert "ollama" in Model.available_providers()
    finally:
        monkeypatch.undo()
        Model.load_registry()


def test_availability_stays_a_pure_local_check(discovered, monkeypatch):
    """Availability must never reach litellm — that can block forever on an interactive auth
    flow. Marking a provider live must not have opened that door."""
    import litellm

    def boom(*_a, **_k):
        raise AssertionError("is_available() must not call litellm")

    monkeypatch.setattr(litellm, "validate_environment", boom, raising=False)
    assert Model.by_id("ollama/kimi-k3:cloud").is_available() is True


def test_a_discovered_model_resolves_through_a_capability_criterion(discovered):
    """No literal-id pin field anymore (the contract carries a criterion, never a model id) — a
    discovered, served model is a genuine candidate for the role's bare capability default,
    reachable without pinning anything (the bundled catalog may still offer a cheaper candidate
    of its own, so this checks membership, not which one wins the price tie-break)."""
    from galaius.config import Config

    config = Config(image_criteria="cap.vlm")
    ranked_ids = [m.id for m in config.ranked_models("image")]
    assert "ollama/kimi-k3:cloud" in ranked_ids


def test_a_per_call_override_still_forces_an_exact_discovered_id(discovered):
    """A literal id has no criteria-syntax equivalent; the per-call ``model=`` override is the
    one remaining way to force an exact self-hosted/discovered model."""
    from galaius.config import Config

    assert Config().resolve_model("image", override="ollama/kimi-k3:cloud") == "ollama/kimi-k3:cloud"


def test_a_discovered_model_is_offered_as_a_vision_candidate(discovered):
    ids = [m.id for m in Model.available_by_capability(ModelCapability.VLM)]
    assert "ollama/kimi-k3:cloud" in ids


def test_a_retired_model_is_never_auto_selected_as_a_vision_model(discovered):
    """We could not read its capabilities, so it must not be guessed into the VLM pool."""
    model = Model.by_id("ollama/qwen3-coder-next:cloud")
    assert model is not None
    assert not model.can(ModelCapability.VLM)


def test_the_baked_catalog_keeps_its_metadata_when_the_same_model_is_pulled(monkeypatch):
    """models.json carries scores the daemon does not know; discovery must ADD to that row,
    never flatten it."""
    baked = next(
        (m for m in Model.registry() if m.provider == "ollama" and m.intelligence_score),
        None,
    )
    assert baked is not None, "expected at least one scored ollama row in models.json"
    name = baked.id.removeprefix("ollama/")
    tags = {"models": [{"name": name, "digest": "d1", "details": {"parameter_size": "1B"}}]}
    show = {name: {"capabilities": ["vision", "completion"]}}

    monkeypatch.setattr(ollama, "discover_cached", lambda: ollama.discover(client=client(tags, show)))
    Model.load_registry()
    try:
        merged = Model.by_id(baked.id)
        assert merged.intelligence_score == baked.intelligence_score
        assert merged.can(ModelCapability.VLM)
        assert merged.is_available() is True
    finally:
        monkeypatch.undo()
        Model.load_registry()


def test_a_catalog_model_the_daemon_does_not_serve_is_not_available(discovered):
    """Discovery makes availability SHARPER, not just broader. models.json lists ~70 Ollama
    models; a key alone made every one look available even though the user pulled three. Once
    the daemon has said what it has, a row it did not name must not be auto-selected."""
    unpulled = next(
        m for m in Model.registry() if m.provider == "ollama" and m.id != "ollama/kimi-k3:cloud"
    )
    assert unpulled.is_available() is False
    assert "ollama/kimi-k3:cloud" in [m.id for m in Model.by_capability(ModelCapability.VLM)]


def test_every_availability_answer_agrees_about_an_unserved_model(discovered):
    """`is_available` and `available_by_capability` must not disagree: the second filters by
    PROVIDER, so once the daemon has named what it serves, a catalog row it did not name would
    still be counted "ready" — and `galaius doctor` would report grounding models that are not
    there."""
    unserved = {
        m.id for m in Model.registry() if m.provider == "ollama" and not m.is_available()
    }
    assert unserved, "the bundled catalog should list ollama models the fixture daemon lacks"

    for cap in (ModelCapability.VLM, ModelCapability.GUI_GROUNDING):
        offered = {m.id for m in Model.available_by_capability(cap)}
        assert not (offered & unserved), f"{cap} offers models the daemon does not serve"
    assert not ({m.id for m in Model.recommended_grounding()} & unserved)


def test_an_explicit_per_call_choice_is_still_respected_for_a_model_not_listed_today(discovered):
    """Availability gates AUTO-selection; an explicit per-call choice is a decision the user
    made. A model the daemon does not list may be one they are about to pull — never walk past
    their choice over it. (No persisted literal-id pin field exists anymore; only the per-call
    ``model=`` override still accepts a bare id.)"""
    from galaius.config import Config

    assert Model.from_litellm_id("ollama/not-pulled-yet").key_missing() is False
    assert Config().resolve_model("image", override="ollama/not-pulled-yet") == "ollama/not-pulled-yet"


def test_a_dead_daemon_leaves_the_baked_catalog_untouched(monkeypatch):
    monkeypatch.setattr(
        ollama, "discover_cached", lambda: ollama.discover(client=failing_client(httpx.ConnectError("no")))
    )
    Model.load_registry()
    try:
        assert Model.by_id("claude-3-7-sonnet-20250219") is not None
        assert Model.by_id("ollama/kimi-k3:cloud") is None
    finally:
        monkeypatch.undo()
        Model.load_registry()


# --- doctor -----------------------------------------------------------------------------------


def test_doctor_names_what_the_daemon_has_pulled(monkeypatch, capsys):
    """`galaius doctor` is where he goes to ask "what will actually run?" — a model he pulled
    and paid for must be named there."""
    from galaius.cli.app import _print_ollama

    monkeypatch.setattr(ollama, "discover_cached", lambda: ollama.discover(client=client()))
    _print_ollama()
    out = capsys.readouterr().out

    assert "kimi-k3:cloud" in out
    assert "vision" in out
    assert "localhost:11434" in out


def test_doctor_says_nothing_when_there_is_no_daemon(monkeypatch, capsys):
    """No Ollama must mean no noise — not a warning line about a feature the user never asked for."""
    from galaius.cli.app import _print_ollama

    monkeypatch.setattr(ollama, "discover_cached", list)
    _print_ollama()
    assert capsys.readouterr().out == ""


def test_a_daemon_serving_only_embeddings_does_not_revoke_the_catalog(monkeypatch):
    """A box with only `bge-m3` pulled is a normal RAG setup. Learning that the daemon has
    nothing we can PROMPT with is not the same as learning the catalog is unusable — it must not
    silently strip key-based availability from every baked row."""
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test-123")
    only_embeddings = [
        ollama.OllamaModel(name="bge-m3:latest", capabilities=("embedding",), digest="d1")
    ]
    monkeypatch.setattr(ollama, "discover_cached", lambda: only_embeddings)
    Model.load_registry()
    try:
        baked = next(m for m in Model.registry() if m.provider == "ollama")
        assert baked.is_available() is True  # the key still speaks for it
    finally:
        monkeypatch.undo()
        Model.load_registry()


# --- the caches -------------------------------------------------------------------------------


def test_a_model_the_budget_skipped_is_not_recorded_as_a_failure(monkeypatch):
    """Only a model we actually ASKED about can have failed. Stamping one the probe budget never
    reached would suppress it for the whole failure window without a single request being made."""
    monkeypatch.setattr(ollama, "_MAX_NEW_PROBES", 1)
    monkeypatch.setattr(ollama, "_open", lambda base: client())
    monkeypatch.setenv("OLLAMA_DISCOVERY", "1")

    ollama.discover_cached()
    stored = ollama.CapabilityCache.model_validate(ollama._CAPABILITY_CACHE.read())

    probed_digest = TAGS["models"][0]["digest"]
    skipped = {row["digest"] for row in TAGS["models"][1:]}
    assert not (set(stored.failures) & skipped), "a model nobody asked about was blamed"
    assert probed_digest in stored.capabilities


def test_a_corrupt_cache_file_never_disables_discovery(monkeypatch):
    """The cache is only rewritten AFTER a successful read, so a read that raises would disable
    discovery permanently rather than for one run."""
    ollama._CAPABILITY_CACHE.write({"fetched_at": "not-a-number", "capabilities": "nonsense"})
    monkeypatch.setattr(ollama, "_open", lambda base: client())
    monkeypatch.setenv("OLLAMA_DISCOVERY", "1")

    assert [m.name for m in ollama.discover_cached()]  # recovered, not dead


def test_a_daemon_row_with_a_hostile_name_is_dropped(monkeypatch):
    """A cloud model's name is written by a third party, printed to a terminal by `doctor` and
    handed to an agent by `list_providers` — so an ANSI escape in it is not a cosmetic issue."""
    hostile = {
        "models": [
            {"name": "\x1b[2J\x1b[31mowned", "digest": "d1", "details": {}},
            {"name": "kimi-k3:cloud", "digest": "d2", "details": {}},
        ]
    }
    found = [m.name for m in ollama.discover(client=client(hostile, {}))]
    assert found == ["kimi-k3:cloud"]


def test_one_bad_probe_never_loses_the_other_models(monkeypatch):
    """The probes run in a thread pool, whose `map` would otherwise propagate the first failure
    and take every other model's answer down with it."""
    real = ollama._probe

    def explode(client_, name):
        if name == "bge-m3:latest":
            raise RuntimeError("boom")
        return real(client_, name)

    monkeypatch.setattr(ollama, "_probe", explode)
    found = {m.name: m for m in ollama.discover(client=client())}
    assert found["kimi-k3:cloud"].vision is True
    assert found["bge-m3:latest"].capabilities == ()


# --- the MCP surface --------------------------------------------------------------------------


async def test_list_providers_names_the_models_the_daemon_serves(monkeypatch):
    """An agent driving galaius over MCP cannot see the user's daemon — so the tool that answers
    "what models can I pass?" has to name them, or they may as well not exist."""
    import json as _json

    from galaius.server.tools_meta import list_providers

    monkeypatch.delenv("GALAIUS_CONFIGURED_PROVIDERS", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(ollama, "discover_cached", lambda: ollama.discover(client=client()))
    Model.load_registry()
    try:
        payload = _json.loads(await list_providers())
    finally:
        monkeypatch.undo()
        Model.load_registry()

    assert "ollama" in payload["available_providers"]  # no key, but it answered
    served = {m["id"]: m for m in payload["ollama"]["models"]}
    assert served["ollama/kimi-k3:cloud"]["vision"] is True
    assert "ollama/bge-m3:latest" not in served  # embeddings are not a model you can pass


# --- against the real daemon ------------------------------------------------------------------


@pytest.mark.integration
def test_the_real_daemon_reports_what_is_pulled():
    """Marked + self-skipping: proves the parsing matches a live Ollama, not just the fixture."""
    if not ollama.reachable():
        pytest.skip("no Ollama daemon reachable")
    models = ollama.discover()
    assert models, "a reachable daemon should list at least one model"
    assert all(m.model_id.startswith("ollama/") for m in models)
    for m in models:
        assert isinstance(m.capabilities, tuple)
