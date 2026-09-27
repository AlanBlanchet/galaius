"""Export operator administration contracts as deterministic JSON Schema."""

import json
from typing import Union

from pydantic import TypeAdapter

from interact_core.admin import (
    BudgetDecision,
    OperatorAuditEvent,
    OperatorAuthority,
    OperatorRunFailure,
    OperatorServiceSummary,
    OperatorWorkspaceSummary,
    SubscriptionPlanDefinition,
    UsageProvenanceSummary,
    UsageRecord,
    WorkspaceSubscription,
    WorkspaceSubscriptionUpdate,
)


def main() -> None:
    contracts = Union[
        BudgetDecision,
        OperatorAuditEvent,
        OperatorAuthority,
        OperatorRunFailure,
        OperatorServiceSummary,
        OperatorWorkspaceSummary,
        SubscriptionPlanDefinition,
        UsageProvenanceSummary,
        UsageRecord,
        WorkspaceSubscription,
        WorkspaceSubscriptionUpdate,
    ]
    print(json.dumps(TypeAdapter(contracts).json_schema(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
