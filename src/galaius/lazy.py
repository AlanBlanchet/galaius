"""Heavy third-party modules bound at import, loaded on first attribute use.

litellm costs ~4 s to import and openai ~1 s. `galaius mcp` boots inside every agent's CLI, and the
CLI holds the agent's first turn until its MCP servers answer: an eager import there is ~4 s of
wait on every agent start, for a dependency only a vision call ever touches.
"""

import importlib.util
import sys
from types import ModuleType


def deferred(name: str) -> ModuleType:
    """`name` as a module object whose code runs on its first attribute access (`LazyLoader`); the
    same object `import name` returns afterwards. Already imported: that module, as it is."""
    if (loaded := sys.modules.get(name)) is not None:
        return loaded
    spec = importlib.util.find_spec(name)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
    spec.loader = importlib.util.LazyLoader(spec.loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
