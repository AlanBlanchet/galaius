#!/usr/bin/env python3
"""`ci_shard.py INDEX COUNT`: the test files one of COUNT CI runners runs, one per line, for `pytest @file`.

Every `tests/**/test_*.py` pytest collects (outside `norecursedirs` in pyproject.toml), sorted, dealt round-robin:
runner INDEX takes files INDEX, INDEX + COUNT, ... Standard library only (Python 3.11+)."""

import sys
import tomllib
from pathlib import Path


def shard(index: int, count: int, root: Path = Path(".")) -> list[str]:
    if not 0 <= index < count:
        raise SystemExit(f"runner {index} of {count}: INDEX runs from 0 to COUNT - 1")
    skipped = [root / folder for folder in tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["norecursedirs"]]
    files = sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py") if not any(path.is_relative_to(folder) for folder in skipped))
    return files[index::count]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print("\n".join(shard(int(sys.argv[1]), int(sys.argv[2]))))
