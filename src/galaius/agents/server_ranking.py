"""A model rule ranked by this PC's server (`AgentRanking`), for a PC with no benchmark board of its
own: the board is fetched with its reader's own key, which a freshly linked PC does not hold, and a
rule ranking on it (`aa.intelligence`) would otherwise clear nothing there."""

from urllib.parse import urlencode

import httpx
from galaius_core import AgentRanking
from pydantic import BaseModel, ConfigDict

from galaius.agents.catalog_connection import CatalogConnection, CatalogConnectionError


class ServerRankingUnavailable(CatalogConnectionError):
    """The server could not rank the rule (unreachable, refused, an answer not read): why, in words."""


class ServerRanking(BaseModel):
    """`connection`'s server's ranking of one rule: the catalog ids clearing it, best first."""

    model_config = ConfigDict(frozen=True)

    connection: CatalogConnection

    @classmethod
    def linked(cls) -> "ServerRanking | None":
        """Through this PC's connection to its server; None when it has none."""
        try:
            connection = CatalogConnection.load()
        except CatalogConnectionError:
            return None
        return None if connection is None else cls(connection=connection)

    def ranked(self, criterion: str, weights: str) -> tuple[str, ...]:
        """The ids clearing `criterion` on the server's board, best first (read at most once per
        `CatalogConnection.REUSE_SECONDS`); `ServerRankingUnavailable` (never kept) when it cannot say."""
        return self.connection.recent(f"agent-ranking\n{criterion}\n{weights}", lambda _previous: self._read(criterion, weights))

    def _read(self, criterion: str, weights: str) -> tuple[str, ...]:
        try:
            with self.connection.connect() as client:
                connection = self.connection.authenticate(client)
                payload = connection.request(client, "GET", f"/v1/workspaces/{connection.workspace_id}/agent-rankings?"
                                             + urlencode({"criterion": criterion, "weights": weights}))
            return AgentRanking.model_validate_json(payload).ranked
        except (CatalogConnectionError, httpx.HTTPError, ValueError) as error:
            raise ServerRankingUnavailable(f"the server's ranking of {criterion!r} is unavailable: {error or type(error).__name__}") from error
