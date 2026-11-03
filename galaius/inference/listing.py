"""Which ids a keyed provider serves today, read from that provider's own model listing.

The bundled catalog keeps ids a provider has retired. Gemini's listing (read 2026-11-01) no longer carries
`gemini-2.0-flash-lite`, `gemma-3-27b-it`, `gemini-exp-1206` or `learnlm-1.5-pro-experimental`, yet
the catalog still priced them at $0 — so a cheapest-first criterion chose them first and every call
answered "gemini does not know this model id", after the fallback chain had spent its slots on them.
A key proves the account exists; only the provider can say which ids it still answers.

Same live-pass shape as the Ollama daemon's (`galaius.inference.ollama`): cached on disk, short
timeout, silent on failure. An unanswered listing restricts nothing — availability falls back to
the key alone, never to "nothing runs".
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from galaius.catalog.ttl_cache import RefreshFailed, TTLCache

_log = logging.getLogger(__name__)

#: One page request. A fetch happens at most once per cache period per key (a failure backs off on
#: disk, `TTLCache.refetch`), on the first availability question after it.
_TIMEOUT = 5.0


class ModelListing(BaseModel):
    """Where one provider lists the ids its API answers, and how to read that answer."""

    model_config = ConfigDict(frozen=True)

    provider: str
    url: str
    #: The key the listing is asked with: the same one a call would spend.
    key_env: str
    key_header: str
    #: The JSON field holding the rows, and the row field naming the id.
    items: str
    id_field: str
    #: What the listing writes before the id a call takes (`models/gemini-2.5-flash`).
    strip: str = ""
    #: The response field carrying the next page's token, and the query parameter sending it back.
    next_page: str = ""
    page_param: str = ""

    @property
    def cache(self) -> TTLCache:
        return TTLCache(f"served_{self.provider}.json", machine_local=True)

    def ids(self, payload: dict[str, Any]) -> list[str]:
        return [str(row[self.id_field]).removeprefix(self.strip) for row in payload[self.items]]

    def fetch(self, key: str) -> frozenset[str]:
        found: set[str] = set()
        params: dict[str, str] = {}
        with httpx.Client(timeout=_TIMEOUT, headers={self.key_header: key}) as client:
            while True:
                response = client.get(self.url, params=params)
                response.raise_for_status()
                payload = response.json()
                found.update(self.ids(payload))
                token = payload.get(self.next_page) if self.next_page else None
                if not token:
                    break
                params = {self.page_param: str(token)}
        if not found:
            raise RefreshFailed(f"{self.provider} listed no model")
        return frozenset(found)

    def served(self) -> frozenset[str] | None:
        """The ids this provider answers with this machine's key; None when it cannot tell (no key,
        listing unreachable with no copy fetched under this key)."""
        key = os.environ.get(self.key_env, "")
        if not key:
            return None
        # Which key the copy was fetched with, never the key: another account lists other ids.
        fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
        cache = self.cache
        stored = cache.read() or {}
        mine = stored.get("key") == fingerprint and isinstance(stored.get("ids"), list)
        if mine and cache.is_fresh(stored):
            return frozenset(stored["ids"])
        try:
            ids = cache.refetch(self.provider, lambda: self.fetch(key))
        except RefreshFailed as failure:
            _log.debug("%s model listing unavailable: %s", self.provider, failure.reason)
            return frozenset(stored["ids"]) if mine else None
        cache.write({"fetched_at": time.time(), "key": fingerprint, "ids": sorted(ids)})
        return ids


#: Providers whose listing answers with the call key. OpenAI is left out: its catalog rows include
#: litellm pricing variants (`1024-x-1024/gpt-image-1.5`, `ft:` templates) its listing never names.
LISTINGS: tuple[ModelListing, ...] = (
    ModelListing(
        provider="gemini",
        url="https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        key_env="GEMINI_API_KEY",
        key_header="x-goog-api-key",
        items="models",
        id_field="name",
        strip="models/",
        next_page="nextPageToken",
        page_param="pageToken",
    ),
)


#: Re-read the listing (disk, then the provider when stale) at most once a minute per key: a ranking
#: asks every catalog row, hundreds of times in one pass.
_MEMO_TTL = 60.0
_MEMO: dict[tuple[str, str], tuple[float, frozenset[str] | None]] = {}
_BY_PROVIDER = {listing.provider: listing for listing in LISTINGS}
_WARNED: set[str] = set()


def served(provider: str) -> frozenset[str] | None:
    """The ids ``provider`` answers with this machine's key; None when it lists nothing we read (not
    a listed provider, no key, listing off or unreachable). Never raises."""
    listing = _BY_PROVIDER.get(provider)
    if listing is None or os.environ.get("MODEL_LISTING", "1").strip().lower() in {"0", "false", "no"}:
        return None
    memo = (provider, os.environ.get(listing.key_env, ""))
    hit = _MEMO.get(memo)
    if hit is not None and time.monotonic() - hit[0] < _MEMO_TTL:
        return hit[1]
    try:
        ids = listing.served()
    except Exception:  # a live source must never break the catalog
        _log.debug("%s model listing failed", provider, exc_info=True)
        ids = None
    if ids is None and memo[1] and provider not in _WARNED:
        # Unread, it restricts nothing: the retired ids rank first again. Said once per process.
        _WARNED.add(provider)
        _log.warning("%s model listing unreadable; its retired catalog ids count as runnable", provider)
    _MEMO[memo] = (time.monotonic(), ids)
    return ids


def warm() -> None:
    """Read every listing now, so a ranking right after only reads memory. Blocks on a stale cache:
    an async caller runs it in a thread."""
    for provider in _BY_PROVIDER:
        served(provider)


__all__ = ["LISTINGS", "ModelListing", "served", "warm"]
