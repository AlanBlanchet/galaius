"""The product is galaius: no command, help text, window title, README, script or extension string spells the former
name « interact » (owner 2026-11-12: « interact ? What is that ? We're on galaius ! »), in any case. The English verb
(interactive, interaction, « interact with ») passes. Tests are not shipped text and are not read."""

import re
import subprocess

from tests.support.paths import REPO

FORMER = re.compile(r"(?<![A-Za-z0-9_])interact(?!ive|ion|ing|s\b|ed\b|or|able|\s+with\b)", re.IGNORECASE)
# Literals a user never sees that must keep the former name, with why.
KEPT = {"galaius/host/private_files.py": 'b"interact.private-files.v1"'}  # unseals what pre-rename builds sealed


def test_no_shipped_text_names_the_former_product() -> None:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True, text=True).stdout.split("\0")
    hits = []
    for path in tracked:
        file = REPO / path
        if not path or path.startswith("tests/") or not file.is_file():
            continue
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        hits += [f"{path}:{number}: {line.strip()[:120]}" for number, line in enumerate(lines, 1)
                 if FORMER.search(line.replace(KEPT.get(path, "\0"), ""))]
    assert not hits, "the former product name in shipped text:\n" + "\n".join(hits)


def test_the_rule_catches_commands_and_titles_and_passes_the_verb() -> None:
    caught = ["run `interact login`", "Interact server", "from interact.client import Client", "interact-core", "interact.exe"]
    passed = ["an interactive map", "user interaction", "agents interact with pages", "interactable", "galaius login"]
    assert [line for line in caught if not FORMER.search(line)] == []
    assert [line for line in passed if FORMER.search(line)] == []
