"""Guards for the automatic model choice (owner 2026-11-23 « check if we are correctly using a form of intelligence / cost graph
to know what model to automatically select »):

- `agents criterion` answers from the SAME ranked list and terms a spawn walks (`rank`): « aa.intelligence and provider ~ claude »
  once answered an Ollama model, because it ranked the whole catalogue and read no provider term;
- a model the vendor says « may not exist » (« There's an issue with the selected model (claude-opus-4-7-20260416) »,
  2026-09-22: both starts died at once) leaves every ranking until its catalogue entry changes.
"""

import json
import os

import pytest

from galaius.agents import providers as agent_providers
from galaius.agents import run as run_module
from galaius.agents.limits import quota
from galaius.catalog import models as mcat
from galaius.cli.commands.agents.models import agents_criterion
from galaius.models.registry import Model
from tests.support.models import catalog_of

REFUSED = "There's an issue with the selected model (vendor-star-1). It may not exist or you may not have access to it."


class _Cli:
    """A CLI running one catalogue provider's models."""

    def __init__(self, name: str) -> None:
        self.name = name

    def can_run(self, model, env):
        return model.provider == self.name

    def model_id_for(self, model):
        return model.id


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("galaius.agents.policy.policy_path", lambda: tmp_path / "agents.json")
    monkeypatch.setenv("FIXTURE_KEY", "set")
    quota.forget()


@pytest.fixture
def two_vendors(tmp_path, monkeypatch):
    """Vendor `mine` (the one a rule names) and vendor `other`, whose model scores higher AND costs less."""
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"scores": [{"name": "Mine Model", "intelligence": 50.0}, {"name": "Other Model", "intelligence": 60.0}]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    clis = {"mine": _Cli("mine"), "other": _Cli("other")}
    monkeypatch.setattr("galaius.agents.providers.PROVIDERS", clis)
    monkeypatch.setattr("galaius.agents.providers.provider_for", lambda name: clis[name])
    with catalog_of(
        Model(id="mine-model", provider="mine", capabilities=set(), intelligence_score=50.0, input_cost_per_million=8.0),
        Model(id="other-model", provider="other", capabilities=set(), intelligence_score=60.0, input_cost_per_million=1.0),
    ):
        Model._provider_keys["mine"] = ["FIXTURE_KEY"]
        Model._provider_keys["other"] = ["FIXTURE_KEY"]
        yield


def _asked(capsys, rule: str, **kwargs) -> dict:
    agents_criterion(rule, json_out=True, **kwargs)
    return json.loads(capsys.readouterr().out)


def test_a_criterion_applies_its_provider_term_and_answers_what_a_spawn_walks_first(two_vendors, capsys):
    rule = "aa.intelligence and provider ~ mine"
    answer = _asked(capsys, rule)
    walked = run_module.rank(rule, dict(os.environ), providers=list(agent_providers.PROVIDERS.values()))
    assert answer["model"] == walked.candidates[0].model == "mine-model", "the preferred CLI's model leads, as it does for a spawn"
    assert answer["provider"] == "mine"
    assert answer["ranked"][0]["model"] == "mine-model"
    # Without the term the list is the other vendor's (higher score, lower price): the term, not luck, moved it.
    assert _asked(capsys, "aa.intelligence")["model"] == "other-model"


def test_a_criterion_a_spawn_would_read_as_a_model_id_names_no_model(two_vendors, capsys):
    answer = _asked(capsys, "provider ~ mine")
    assert answer["model"] is None and "model id" in answer["why"]


def test_a_criterion_nothing_runs_names_no_model_and_says_why(two_vendors, capsys):
    answer = _asked(capsys, "aa.intelligence > 99")
    assert answer["model"] is None and answer["why"]


def test_the_vendors_words_about_an_unknown_model_are_a_refusal_of_that_model():
    assert quota.classify(REFUSED) == "model_capability_unsupported"
    assert quota.classify("There's an issue with the selected model (x). Try again later.") is None


def test_a_model_the_vendor_says_may_not_exist_leaves_every_ranking_until_its_catalogue_entry_changes(two_vendors, capsys):
    rule = "aa.intelligence"
    assert _asked(capsys, rule)["model"] == "other-model"
    quota.remember("model_capability_unsupported", "other", "other-model", said=REFUSED, login="")
    assert ("other", "other-model") in quota.withdrawn()
    after = _asked(capsys, rule)
    assert after["model"] == "mine-model", "the refused model is no first choice"
    assert "other-model" not in json.dumps(after["ranked"]), "and no fallback behind it either"
    # The entry changes (a new price): the vendor is asked again.
    Model._register(Model(id="other-model", provider="other", capabilities=set(), intelligence_score=60.0, input_cost_per_million=2.0))
    assert ("other", "other-model") not in quota.withdrawn()
    assert _asked(capsys, rule)["model"] == "other-model"


def test_a_new_board_score_alone_does_not_ask_a_withdrawn_model_again(two_vendors):
    quota.remember("model_capability_unsupported", "other", "other-model", said=REFUSED, login="")
    Model._register(Model(id="other-model", provider="other", capabilities=set(), intelligence_score=61.0, input_cost_per_million=1.0))
    assert ("other", "other-model") in quota.withdrawn()


def test_an_answer_from_a_withdrawn_model_lifts_it_but_one_heard_before_the_refusal_does_not(two_vendors):
    import time

    quota.remember("model_capability_unsupported", "other", "other-model", said=REFUSED, login="")
    quota.served("other", "other-model", at=time.time() - 60)
    assert ("other", "other-model") in quota.withdrawn()
    quota.served("other", "other-model")
    assert ("other", "other-model") not in quota.withdrawn()


def test_a_ranking_is_never_emptied_by_what_was_withdrawn(two_vendors, capsys):
    for provider, model in (("mine", "mine-model"), ("other", "other-model")):
        quota.remember("model_capability_unsupported", provider, model, said=REFUSED, login="")
    assert _asked(capsys, "aa.intelligence")["model"] is not None


def test_a_plain_refusal_of_a_login_withdraws_nothing(two_vendors):
    quota.remember("model_capability_unsupported", "other", "other-model",
                   said="The 'other-model' model is not supported when using Codex with a ChatGPT account.", login="acct")
    assert quota.withdrawn() == frozenset()
    assert quota.unsupported_until("other", "other-model", login="acct") is not None

