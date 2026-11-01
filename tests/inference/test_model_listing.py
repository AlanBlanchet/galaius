"""A keyed provider's own model listing rules out the catalog ids it retired.

The case that motivated it: Gemini no longer lists `gemini-2.0-flash-lite`, yet the catalog priced it
at $0, so a cheapest-first criterion chose it and every call answered "unknown model id".
"""

import pytest

from galaius.inference import listing
from galaius.models.registry import Model


@pytest.fixture
def listed(monkeypatch, tmp_path):
    """The bundled catalog, a Gemini key, listings on, their cache in a scratch home."""
    Model.load_registry()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MODEL_LISTING", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "key-a")
    listing._MEMO.clear()
    yield
    listing._MEMO.clear()


@pytest.fixture
def gemini_lists(listed, monkeypatch):
    """Gemini answering its listing with one current id."""
    asked: list[str] = []

    def fetch(self, key):
        asked.append(key)
        return frozenset({"gemini-2.5-flash"})

    monkeypatch.setattr(listing.ModelListing, "fetch", fetch)
    return asked


@pytest.mark.parametrize(
    ("model_id", "available"),
    [
        ("gemini/gemini-2.5-flash", True),  # listed
        ("gemini/gemini-2.0-flash-lite", False),  # retired: the provider no longer lists it
        ("gemini/gemini-2.5-flash-preview-09-2025", False),  # a sibling spelling never rides on the listed one
    ],
)
def test_a_listed_provider_rules_out_what_it_no_longer_lists(gemini_lists, model_id, available):
    row = Model.by_id(model_id) or Model.named(model_id)
    assert row.is_available() is available


def test_the_listing_is_cached_per_key_and_never_replaces_the_key(gemini_lists, monkeypatch):
    row = Model.named("gemini/gemini-2.5-flash")
    assert row.is_available() and row.is_available()
    listing._MEMO.clear()
    assert row.is_available()
    assert gemini_lists == ["key-a"]  # second process read the disk copy
    monkeypatch.setenv("GEMINI_API_KEY", "key-b")
    assert row.is_available()
    assert gemini_lists == ["key-a", "key-b"]  # another account lists its own ids
    monkeypatch.delenv("GEMINI_API_KEY")
    assert not row.is_available()


def test_an_unreachable_listing_restricts_nothing(listed, monkeypatch):
    def down(self, key):
        raise listing.RefreshFailed("gemini unreachable")

    monkeypatch.setattr(listing.ModelListing, "fetch", down)
    assert Model.named("gemini/gemini-2.0-flash-lite").is_available()
