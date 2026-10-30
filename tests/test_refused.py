"""The runtime's refusals on the one coded-refusal base (galaius-core `Refused`): each kind declares only its reason's type,
a bare reason is validated into it at the raise, and the kinds a machine answer carries reach it whole (`refusal_of`)."""
from datetime import UTC, datetime

import pytest
from galaius_core import AgentRefusal, Refused, SubscriptionCap, SubscriptionRefusal, refusal_of
from pydantic import ValidationError

from galaius.agents.host import ConversationRefused
from galaius.agents.subscriptions import SubscriptionCapped, SubscriptionRefused
from galaius.machine_programs import ProgramFailed
from galaius_core import AgentRefused
from galaius.workspace_git import WorkspaceRefused

CAP = SubscriptionCap(window="five_hour", used_percent=81, cap_percent=80, at_cap="finish", resets_at=datetime(2026, 10, 26, 15, tzinfo=UTC))


@pytest.mark.parametrize(("raised", "answered"), [
    (WorkspaceRefused("git_missing", "git is not installed on this PC"), AgentRefusal(reason="git_missing")),
    (AgentRefused("web_full", "4 conversations started from the web are already working"), AgentRefusal(reason="web_full")),
    (AgentRefused("under_way", "its first delivery is still under way"), AgentRefusal(reason="under_way")),
    (SubscriptionRefused("lent", "a member's run here runs only inside its fence"), SubscriptionRefusal(reason="lent")),
    (SubscriptionCapped(CAP), SubscriptionRefusal(reason="capped", cap=CAP)),
    (ProgramFailed("download_failed", "too many redirects"), None),        # a program job's failure: never on an answer
    (ConversationRefused("conflict", "Conversation already has an active turn."), None),
])
def test_a_refusal_carries_its_typed_reason_and_its_sentence(raised, answered) -> None:
    assert isinstance(raised, PermissionError) and refusal_of(raised) == answered and str(raised)
    kind = SubscriptionRefused if isinstance(raised, SubscriptionCapped) else type(raised)   # a cap builds its own refusal
    with pytest.raises(ValidationError):
        kind("no_such_reason", "a reason its kind does not know is refused at the raise")
