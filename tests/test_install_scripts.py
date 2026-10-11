"""The install scripts' report part (`galaius/data/install_report.*`): the repository's install.sh / install.ps1
carry it verbatim (a Galaius server inserts the same file into the scripts it serves); its JSON text survives
any log line, its log tail hides the home folder, and a program an install leaves running never stalls it."""

import json
import os
import re
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PART = files("galaius") / "data"
posix = pytest.mark.skipif(sys.platform == "win32", reason="install.sh runs on Linux and macOS")


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_each_install_script_carries_the_report_part_verbatim(script: str) -> None:
    text = (ROOT / script).read_text(encoding="utf-8")
    carried = re.search(r"# >>> install report[^\n]*\n(.*)# <<< install report", text, re.S)
    assert carried is not None and carried[1] == (PART / f"install_report{Path(script).suffix}").read_text(encoding="utf-8")
    assert script.endswith(".sh") or text.isascii(), "Windows PowerShell may read install.ps1 as Latin-1"


def sh(program: str, *, env: dict[str, str] | None = None, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """`program` run by sh with the report part loaded."""
    return subprocess.run(["sh", "-c", f'. "$0"; {program}', str(PART / "install_report.sh")], input=stdin, capture_output=True,
                          env={**os.environ, **(env or {})}, timeout=30, check=True)


@posix
@pytest.mark.parametrize("line", [
    'C:\\Users\\Zoé\\AppData "quoted"\ttab',
    "uv: \x1b[31merror\x1b[0m: Failed\r",
    "x" * 589 + "é tail",           # the 590-byte cut falls inside a two-byte character
    "",
])
def test_a_log_line_is_always_valid_json_text(line: str) -> None:
    text = sh("json_text", stdin=line.encode() + b"\n").stdout.decode()
    decoded = json.loads(f'"{text}"')
    assert decoded == "".join(c for c in line if c in "\t" or ord(c) >= 32 and ord(c) != 127).replace("\x1b", "") + "\n"
    first = sh("first_590 | json_text", stdin=line.encode() + b"\n").stdout
    assert len(first) <= 600 * 2 and json.loads(f'"{first.decode()}"').rstrip("\n") in line.replace("\x1b", "").replace("\r", "")


@posix
def test_the_log_tail_writes_the_home_folder_as_a_tilde(tmp_path: Path) -> None:
    home = tmp_path / "a.b|c"
    log = tmp_path / "install.log"
    log.write_text(f"10:00:01 installing to {home}/.local/share/galaius/uv\n10:00:02 elsewhere: /tmp/a.bXc\n")
    tail = sh('log_tail', env={"HOME": str(home), "log": str(log)}).stdout.decode()
    assert tail == "10:00:01 installing to ~/.local/share/galaius/uv\n10:00:02 elsewhere: /tmp/a.bXc\n"


@posix
def test_a_program_left_running_never_stalls_the_install(tmp_path: Path) -> None:
    """`galaius login` opens the browser, which keeps the output it inherited: through `through_file` the install
    still ends at once, its log complete (code-reviewer 2026-10-11 r1)."""
    program = ("describe() { printf 'test install'; }; "
               "main() { step 'connecting this computer' 'la connexion'; through_file sh -c 'echo opened; sleep 5 &'; }; run")
    started = time.monotonic()
    shown = sh(program, env={"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data"), "LANG": "C"})
    assert time.monotonic() - started < 3 and shown.stdout.decode() == "opened\n"
    log = next((tmp_path / "data/galaius/logs").glob("install-*.log")).read_text()
    assert [line.split(" ", 2)[2] for line in log.splitlines()][::2] == ["test install", "-- connecting this computer", "done"]
