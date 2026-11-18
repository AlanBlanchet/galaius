"""The folder `conftest` puts first on `PYTHONPATH`: its `sitecustomize` keeps a test's child Python processes out of every
run scope (see `sitecustomize`). `python -I` ignores `PYTHONPATH`: `isolated_module` starts a module isolated yet patched."""

import sys
from pathlib import Path

CHILD_SITE = Path(__file__).resolve().parent

_BOOT = ("import sys; sys.path.insert(0, {site!r}); import sitecustomize, runpy; module = sys.argv.pop(1); "
         "runpy.run_module(module, run_name='__main__', alter_sys=True)")


def isolated_module(module: str, *arguments: str) -> list[str]:
    """`python -I -m module arguments`, the child patched as every test child is; `module` stays in its command line."""
    return [sys.executable, "-I", "-c", _BOOT.format(site=str(CHILD_SITE)), module, *arguments]
