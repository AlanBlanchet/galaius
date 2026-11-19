"""No tracked file name holds more than two words (snake_case, kebab-case and camelCase all count; a `test_` prefix does not).

Owner, 2026-11-18: « I hate it when there are more than two words inside a file name. I prefer having a folder with a
concept, inside that folder the file specific. »
"""

import re
import subprocess
from pathlib import PurePosixPath

from tests.support.paths import REPO

LIMIT = 2
# Names a tool, a vendor or an installed build fixes, with who fixes them.
EXEMPT_FOLDERS = {
    "galaius/agents/transport/codex_app_server_schema": "captured upstream Codex schema, named by the upstream",
}
EXEMPT_FILES = {
    "clients/vscode/media/world/LICENSE-roguelike-characters.txt": "licence file shipped with the art pack it covers",
    "clients/vscode/media/world/LICENSE-roguelike-indoors.txt": "licence file shipped with the art pack it covers",
    "clients/vscode/media/world/LICENSE-rpg-urban-pack.txt": "licence file shipped with the art pack it covers",
    "galaius/data/release-keys/galaius-release-2.pub": "key file named by its key id",
    "galaius/machine/sign_in_browser.py": "console-script target of installed builds (tests/upgrade/test_shipped_names.py)",
    "tests/fixtures/prompt_source/hooks/turn-end-asks.md": "the hook's product file name, resolved by galaius/prompts/projection.py",
}


def _words(name: str) -> list[str]:
    stem = name.split(".")[0] if not name.startswith(".") else name
    stem = stem.removeprefix("test_")
    return [word for word in re.split(r"[_\-]|(?<=[a-z0-9])(?=[A-Z])", stem) if word]


def test_no_tracked_file_name_has_more_than_two_words() -> None:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True, text=True).stdout.split("\0")
    tracked = [path for path in tracked if path]
    long = [
        path for path in tracked
        if len(_words(PurePosixPath(path).name)) > LIMIT
        and path not in EXEMPT_FILES
        and str(PurePosixPath(path).parent) not in EXEMPT_FOLDERS
    ]
    assert not long, "file names over two words (rename, or a folder naming the concept):\n" + "\n".join(sorted(long))
    assert all(path in tracked for path in EXEMPT_FILES), f"exempt file gone, drop it: {set(EXEMPT_FILES) - set(tracked)}"
