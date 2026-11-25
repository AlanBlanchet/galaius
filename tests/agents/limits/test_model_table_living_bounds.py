"""GUARD cells the first living file lacked (tester aa8f5f6b, 2026-11-25): a shut login is asked a BOUNDED number of times, and a refusal
never repeats the same (model, login) turn. Same stand-in, same fixture as `test_model_table_living`."""

import pytest

from tests.agents.limits.test_model_table_living import (  # noqa: F401 — the fixture is reused by name
    TABLE, _continue, _launch, _out, standin,
)


@pytest.mark.parametrize("cause", ["week", "login_lost"])
async def test_a_login_shut_whole_is_asked_once_per_model_at_most_never_a_storm(standin, cause: str) -> None:
    """Every model of the one login is out: the turn that follows his message asks each model of the table at most once (the table's
    length), then stops and says so. Red on e31c445d: nine turns (a, b, c, c, c, c, c, c, a)."""
    plan, asked, tmp_path = standin
    plan(**_out(cause, 2))
    run = await _launch(tmp_path)
    _continue(run.run_id, "operator")
    _continue(run.run_id, "operator")
    assert len(asked()) - 1 <= 2 * len(TABLE), f"asked {asked()}"


async def test_one_models_refusal_is_never_asked_twice_in_a_row_while_another_model_answers(standin) -> None:
    plan, asked, tmp_path = standin
    plan(**_out("overloaded", 2))
    run = await _launch(tmp_path)
    _continue(run.run_id, "operator")
    _continue(run.run_id, "operator")
    after_work = asked()[1:]
    assert after_work.count("model-a") <= 1, f"the refused model was asked again and again: {asked()}"
