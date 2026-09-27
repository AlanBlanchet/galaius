#!/usr/bin/env python3
"""Hanging ffmpeg fixture with a descendant, for process-group cancellation tests."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> None:
    if sys.argv[1:] == ["sleep-child"]:
        time.sleep(30)
        return
    child = subprocess.Popen(
        [sys.executable, __file__, "sleep-child"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marker = Path(__file__).with_name("ffmpeg-pids.json")
    marker.write_text(json.dumps({"ffmpeg": os.getpid(), "child": child.pid}))
    time.sleep(2.5)


if __name__ == "__main__":
    main()
