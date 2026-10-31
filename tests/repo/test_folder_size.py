"""No tracked folder holds more than 20 files; a bigger one needs a parent that names its domain.

Owner, 2026-10-31: « We should never have like more than 20 files in a folder. I think we should instead try to find correct
parents, correct parent folders. »
"""

import subprocess
from collections import Counter
from pathlib import PurePosixPath

from tests.support.paths import REPO

LIMIT = 20
# Folders a tool writes, with the tool: their file count follows the upstream, not our layout.
EXEMPT = {
    "src/galaius/agents/transport/codex_app_server_schema": "captured upstream Codex schema, written by scripts/refresh_codex_schema_capture.py",
}


def test_no_tracked_folder_holds_more_than_20_files() -> None:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True, text=True).stdout.split("\0")
    sizes = Counter(str(PurePosixPath(path).parent) for path in tracked if path)
    over = {folder: count for folder, count in sizes.items() if count > LIMIT and folder not in EXEMPT}
    assert not over, "folders over 20 files (find each its domain sub-folders):\n" + "\n".join(f"{count:4} {folder}" for folder, count in sorted(over.items()))
    assert all(folder in sizes for folder in EXEMPT), f"exempt folder gone, drop it from EXEMPT: {set(EXEMPT) - sizes.keys()}"
