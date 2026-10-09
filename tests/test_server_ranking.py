"""A freshly linked PC has no benchmark board (it is fetched with its reader's own key): a role
ranking on it (`aa.intelligence`) launches in the order its server ranks, a model newer than its own
catalog included, never nothing."""

import pytest

from galaius.agents import run
from galaius.agents.providers import PROVIDERS
from galaius.agents.server_ranking import ServerRanking, ServerRankingUnavailable
from galaius.models import Model


def test_a_pc_without_a_board_launches_its_servers_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCHMARK_SCORES", "")
    env: dict[str, str] = {}
    claude = PROVIDERS["claude"]
    known = [row for row in Model.catalog() if claude.can_run(row, env)][0].catalog_id
    newest = "anthropic/claude-newer-than-this-catalog"
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(ServerRanking, "linked", classmethod(lambda cls: cls.model_construct()))
    monkeypatch.setattr(ServerRanking, "ranked", lambda self, criterion, weights: asked.append((criterion, weights)) or (newest, known))
    candidates = run.rank_candidates("aa.intelligence", env, providers=[claude])
    assert [(c.provider, c.catalog_id) for c in candidates] == [("claude", newest), ("claude", known)] and asked == [("aa.intelligence", "")]
    assert all(row.catalog_id != newest for row in Model.catalog())  # launched, never added to what this PC ranks itself


def test_a_server_that_cannot_rank_is_named_in_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCHMARK_SCORES", "")
    monkeypatch.setattr(ServerRanking, "linked", classmethod(lambda cls: cls.model_construct()))

    def unreachable(self, criterion, weights):
        raise ServerRankingUnavailable("the server's ranking of 'aa.intelligence' is unavailable: ConnectError")

    monkeypatch.setattr(ServerRanking, "ranked", unreachable)
    with pytest.raises(run.ModelUnavailable, match="no benchmark board of its own, and the server's ranking .* unavailable: ConnectError"):
        run.rank_candidates("aa.intelligence", {}, providers=[PROVIDERS["claude"]])
