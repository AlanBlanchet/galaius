"""A test's child Python processes live in no run's scope either (`conftest._in_no_run_scope`).

The fixture patches the test process only. A child it starts (`sys.executable -c …`, the `galaius` script) reads its own
cgroup: inside a `systemd-run` service (the pre-push hook's) it is `Caller.unowned`, inside an agent's shell it acts as
that agent's run, and neither happens on GitHub's runners. `conftest` puts this folder first on `PYTHONPATH`, so
every child imports this module at start-up, which applies the same patch once `galaius.account.caller` is imported
(a lazy hook: no galaius import before the child's own).

The patch is a test aid only: no production code reads it, and the refusal of a really unowned caller is exercised
with the caller's functions patched to say so (`tests/account/test_caller_scope.py`)."""

import importlib.abc
import sys
from collections.abc import Callable
from types import ModuleType

CALLER_MODULE = "galaius.account.caller"


def patch_caller(module: ModuleType, setter: Callable[[object, str, object], None] = setattr) -> None:
    """`module` (`galaius.account.caller`) reads no unit and no run scope: its process is the person's terminal."""
    setter(module, "process_unit", lambda pid: None)
    setter(module, "run_of_process", lambda pid: None)
    setter(module.Caller, "_for_no_run", False)


class _PatchAfterImport(importlib.abc.MetaPathFinder):
    """Finds `CALLER_MODULE` through the other finders and patches it right after it executes."""

    def find_spec(self, name, path, target=None):
        if name != CALLER_MODULE:
            return None
        sys.meta_path.remove(self)
        for finder in sys.meta_path:
            spec = finder.find_spec(name, path, target) if hasattr(finder, "find_spec") else None
            if spec is not None and spec.loader is not None:
                execute = spec.loader.exec_module

                def exec_module(module: ModuleType, execute=execute) -> None:
                    execute(module)
                    patch_caller(module)

                spec.loader.exec_module = exec_module
                return spec
        return None


if CALLER_MODULE not in sys.modules:
    sys.meta_path.insert(0, _PatchAfterImport())
