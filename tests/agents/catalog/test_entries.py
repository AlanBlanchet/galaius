"""A delegate capability asked by any spelling of its role: two launches failed on « declares no delegate capability 'visual-critic' » and « 'ask_visual-critic' »
while the catalog declared `ask_visual_critic`."""
from uuid import uuid4

import pytest

from galaius.agents.catalog.connection import CatalogConnectionError
from galaius.agents.catalog.entries import CatalogSnapshot
from tests.agents.catalog.test_agent_catalog import snapshot


def catalog() -> tuple[CatalogSnapshot, object]:
    """The fixture catalog with its lead declaring `ask_visual_critic`."""
    held = CatalogSnapshot.model_validate(snapshot().model_dump())
    lead = held.role("fixture-lead")
    declared = lead.capabilities[0].model_copy(update={"name": "ask_visual_critic"})
    agents = tuple(agent.model_copy(update={"capabilities": (declared,)}) if agent.id == lead.id else agent for agent in held.agents)
    return held.model_copy(update={"agents": agents}), declared.agent


@pytest.mark.parametrize("asked", ["ask_visual_critic", "ask_visual-critic", "visual-critic", "visual_critic", "ask-visual-critic"])
def test_a_delegate_asked_by_any_spelling_of_its_role_is_the_one_the_catalog_declares(asked: str) -> None:
    held, pin = catalog()
    assert held.delegate_name(asked) == "ask_visual_critic" and held.delegated(asked) == pin


def test_a_delegate_no_spelling_matches_is_refused_naming_the_declared_ones() -> None:
    held, _pin = catalog()
    with pytest.raises(CatalogConnectionError, match="no delegate capability 'critic'.*declared: ask_visual_critic"):
        held.delegated("critic")
