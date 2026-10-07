"""Authenticated typed prompt-catalog synchronization for the local cache."""

import json
import urllib.error
import urllib.request
from urllib.parse import quote

from galaius_core import (
    PromptCatalogPage,
    PromptChannelEntry,
    PromptExecutionRef,
    PromptRevision,
    PromptSelection,
)

from galaius import USER_AGENT
from galaius.prompt_cache import _PromptCache


class _PromptClient:
    _MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, endpoint: str, token: str) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token = token

    def sync(self, account: str, cache: _PromptCache) -> PromptCatalogPage:
        page = PromptCatalogPage.model_validate(self._get("/v1/catalog"))
        revisions = tuple(self._revision(entry) for entry in page.entries)
        cache.apply(account, page, revisions)
        return page

    def resolve(
        self, account: str, cache: _PromptCache, selection: PromptSelection,
    ) -> tuple[str, PromptExecutionRef]:
        try:
            self.sync(account, cache)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError):
            pass
        content, resolved = cache.resolve_execution(
            account, selection.key, selection.channel, selection.digest
        )
        return content, resolved

    def _revision(self, entry: PromptChannelEntry) -> PromptRevision:
        path = "/v1/revisions/{}/{}/{}".format(
            quote(entry.key.namespace, safe=""),
            quote(entry.key.slug, safe=""),
            entry.digest,
        )
        return PromptRevision.model_validate(self._get(path))

    def _get(self, path: str) -> object:
        request = urllib.request.Request(
            self._endpoint + path,
            headers={"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read(self._MAX_RESPONSE_BYTES + 1)
            if len(body) > self._MAX_RESPONSE_BYTES:
                raise ValueError("prompt service response exceeds the size limit")
            return json.loads(body)
