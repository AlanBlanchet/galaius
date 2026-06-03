"""A self-contained, WM-independent draggable window for the desktop test suite.

Xephyr ships no window manager, so a normal toplevel has no title bar to grab. This
window draws its own: ``overrideredirect(True)`` strips any decorations, a coloured
``Frame`` plays the title bar, and dragging it moves the whole toplevel via ``geometry``
— exactly the "click the top bar and move the window" interaction, with zero gi/WM deps.

Each move appends the window's top-left ``x,y`` to the file given as ``argv[1]`` so a
test can read back the path the window travelled (e.g. assert it traced a circle).

    python drag_window.py /path/to/positions.txt [WIDTHxHEIGHT+X+Y]
"""

import os
import sys
from pathlib import Path


def _ensure_tcltk() -> None:
    """Point Tcl/Tk at the libraries bundled with this interpreter.

    uv's standalone CPython ships Tk but bakes in a build-time ``TCL_LIBRARY`` that
    doesn't exist at runtime, so ``tkinter`` can't find ``init.tcl``. Deriving the path
    from ``sys.base_prefix`` fixes that; on a system Python (paths compiled in) the env
    vars are already set or the globs simply find the same dirs — harmless either way.
    """
    lib = Path(sys.base_prefix) / "lib"
    for var, marker, pattern in (
        ("TCL_LIBRARY", "init.tcl", "tcl8.*"),
        ("TK_LIBRARY", "tk.tcl", "tk8.*"),
    ):
        if os.environ.get(var):
            continue
        for cand in sorted(lib.glob(pattern), reverse=True):
            if (cand / marker).is_file():
                os.environ[var] = str(cand)
                break


_ensure_tcltk()

import tkinter as tk  # noqa: E402 — must follow the Tcl/Tk env bootstrap above

BAR_H = 32
GEOMETRY = sys.argv[2] if len(sys.argv) > 2 else "320x220+140+120"
pos_file = sys.argv[1] if len(sys.argv) > 1 else None


def main() -> None:
    root = tk.Tk()
    root.title("interact-drag-window")  # xdotool search --name target
    root.overrideredirect(True)  # no WM needed: we are our own decoration
    root.geometry(GEOMETRY)

    bar = tk.Frame(root, bg="#3060c0", height=BAR_H, cursor="fleur")
    bar.pack(fill="x", side="top")
    tk.Label(bar, text="≡ Drag Me", fg="white", bg="#3060c0", font=("TkDefaultFont", 11, "bold")).pack(
        side="left", padx=10, pady=4
    )
    tk.Label(root, text="window body", bg="white").pack(expand=True, fill="both")

    grab = {"x": 0, "y": 0}

    def start(event: "tk.Event") -> None:
        grab["x"], grab["y"] = event.x, event.y

    def drag(event: "tk.Event") -> None:
        x = root.winfo_x() + event.x - grab["x"]
        y = root.winfo_y() + event.y - grab["y"]
        root.geometry(f"+{x}+{y}")
        if pos_file:
            with open(pos_file, "a") as handle:
                handle.write(f"{x},{y}\n")

    bar.bind("<Button-1>", start)
    bar.bind("<B1-Motion>", drag)
    root.mainloop()


if __name__ == "__main__":
    main()
