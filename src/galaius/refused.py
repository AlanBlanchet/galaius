"""What this computer refuses, typed once (`Refused`): the reason, for the server to word in its reader's language (this
computer cannot know it), and this computer's own English sentence for its log (`str(error)`)."""

from typing import Any, ClassVar, Generic, TypeVar, get_args

from galaius_core import MachineRefusal
from pydantic import BaseModel, TypeAdapter

R = TypeVar("R")
#: The kinds a machine answer carries (`MachineAgentAnswer.refusal`): an agent, a subscription or a screen refusal.
ANSWERED: tuple[type[BaseModel], ...] = get_args(get_args(MachineRefusal)[0])


class Refused(PermissionError, Generic[R]):
    """A refusal of this computer's: `refusal` its reason, typed `R`. A kind declares its type parameter alone
    (`class WorkspaceRefused(Refused[AgentRefusal])`); a bare reason builds that kind's model, validated here
    (`WorkspaceRefused("git_missing", "git is not installed on this PC")`)."""

    _reasons: ClassVar[TypeAdapter[Any]]
    _model: ClassVar[bool]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        declared = next((get_args(base)[0] for base in cls.__dict__.get("__orig_bases__", ()) if getattr(base, "__origin__", None) is Refused), None)
        if declared is not None:
            cls._reasons = TypeAdapter(declared)
            cls._model = isinstance(declared, type) and issubclass(declared, BaseModel)

    def __init__(self, refusal: R | str, sentence: str) -> None:
        super().__init__(sentence)
        self.refusal: R = self._reasons.validate_python({"reason": refusal} if self._model and isinstance(refusal, str) else refusal)

    @property
    def answered(self) -> MachineRefusal | None:
        """It as a machine answer carries it (`MachineAgentAnswer.refusal`); None for a kind the answer never holds."""
        return self.refusal if isinstance(self.refusal, ANSWERED) else None
