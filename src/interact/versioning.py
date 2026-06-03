"""Single source of truth for the project version, plus semver helpers.

``pyproject.toml`` ``[project].version`` is authoritative; ``vscode-extension/package.json``
``version`` must match it. These helpers power the release skill, the pre-commit sync
check, the CI auto-tagger, and the TUI/CLI update check — so a bump is mechanical and the
two files can't drift.

Run directly:

    python -m interact.versioning current        # print the version
    python -m interact.versioning check           # exit 1 if the two files disagree
    python -m interact.versioning bump minor       # bump both files, print the new version
"""

import json
import re
import sys
from pathlib import Path

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def parse(version: str) -> tuple[int, int, int]:
    """``"1.2.3"`` / ``"v1.2.3"`` → ``(1, 2, 3)``; raises on non-semver."""
    match = _SEMVER.match(version.strip())
    if not match:
        raise ValueError(f"not a semantic version: {version!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, baseline: str) -> bool:
    """True if ``candidate`` is a strictly higher semver than ``baseline`` (never raises)."""
    try:
        return parse(candidate) > parse(baseline)
    except ValueError:
        return False


def bump(version: str, part: str) -> str:
    major, minor, patch = parse(version)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"part must be major|minor|patch, got {part!r}")


def repo_root(start: Path | None = None) -> Path:
    """Nearest ancestor containing pyproject.toml (the project root)."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise FileNotFoundError("no pyproject.toml in any parent directory")


def pyproject_version(root: Path) -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text())
    if not match:
        raise ValueError("no version field in pyproject.toml")
    return match.group(1)


def package_json_version(root: Path) -> str | None:
    """The extension's version, or None if there's no extension package.json."""
    package = root / "vscode-extension" / "package.json"
    if not package.exists():
        return None
    return json.loads(package.read_text()).get("version")


def check_in_sync(root: Path) -> list[str]:
    """Problems that should block a commit/release — empty list means all good."""
    try:
        py = pyproject_version(root)
        parse(py)
    except (ValueError, FileNotFoundError) as exc:
        return [str(exc)]
    extension = package_json_version(root)
    if extension is not None and extension != py:
        return [f"version mismatch: pyproject.toml={py} but vscode-extension/package.json={extension}"]
    return []


def set_version(root: Path, version: str) -> None:
    """Write ``version`` into both pyproject.toml and the extension package.json."""
    parse(version)  # validate before touching files
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        re.sub(r'(?m)^(version\s*=\s*")[^"]+(")', rf"\g<1>{version}\g<2>", pyproject.read_text(), count=1)
    )
    package = root / "vscode-extension" / "package.json"
    if package.exists():
        package.write_text(
            re.sub(r'("version"\s*:\s*")[^"]+(")', rf"\g<1>{version}\g<2>", package.read_text(), count=1)
        )


def force_utf8_io() -> None:
    """Make stdout/stderr UTF-8 so glyphs (✓, →, …) don't crash on Windows' cp1252 console
    with UnicodeEncodeError. No-op where already UTF-8 (Linux/macOS)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    root = repo_root()
    if command == "current":
        print(pyproject_version(root))
    elif command == "check":
        errors = check_in_sync(root)
        if errors:
            print("\n".join(f"✗ {e}" for e in errors), file=sys.stderr)
            return 1
        print(f"✓ version in sync: {pyproject_version(root)}")
    elif command == "bump":
        if len(argv) < 2:
            print("usage: bump <major|minor|patch>", file=sys.stderr)
            return 2
        new = bump(pyproject_version(root), argv[1])
        set_version(root, new)
        print(new)
    else:
        print(f"unknown command {command!r}; use current|check|bump", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
