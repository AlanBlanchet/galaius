"""Export the public prompt contracts as one deterministic JSON Schema document."""

import json
from typing import Union

from pydantic import TypeAdapter

from interact_core import (
    PromptCatalogPage,
    PromptChannelEntry,
    PromptCreateRequest,
    PromptExecutionRef,
    PromptKey,
    PromptPublicationRequest,
    PromptRevision,
    PromptSelection,
)


def main() -> None:
    contract = Union[
        PromptKey, PromptRevision, PromptChannelEntry, PromptCatalogPage, PromptSelection,
        PromptCreateRequest, PromptExecutionRef, PromptPublicationRequest,
    ]
    print(json.dumps(TypeAdapter(contract).json_schema(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
