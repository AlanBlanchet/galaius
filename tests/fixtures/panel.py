"""A labelled control panel for the desktop scenario — detection + interaction target.

Big, clearly-labelled widgets a grounding model can find ("Click Me", "Increment",
"Reset", a text field), each writing the resulting app state to a JSON file (``argv[1]``)
so a test can assert an interaction actually landed — no VLM needed to verify. WM-less
(``overrideredirect``) so it runs under a bare Xephyr; spawn it with a Tk-capable Python.

    python panel.py /path/to/state.json [WIDTHxHEIGHT+X+Y]
"""

import json
import os
import sys
from pathlib import Path


def _ensure_tcltk() -> None:
    """Point Tcl/Tk at this interpreter's bundled libraries (see drag_window.py)."""
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

import tkinter as tk  # noqa: E402 — must follow the Tcl/Tk env bootstrap

GEOMETRY = sys.argv[2] if len(sys.argv) > 2 else "360x420+120+90"
state_file = sys.argv[1] if len(sys.argv) > 1 else None


def main() -> None:
    state = {"last": "", "count": 0, "typed": ""}

    def persist() -> None:
        if state_file:
            Path(state_file).write_text(json.dumps(state))

    root = tk.Tk()
    root.title("interact-panel")
    # A normal, WM-managed window (not override-redirect): on a real desktop the window
    # manager only grants keyboard focus to managed windows, so a click can focus the
    # field and typing lands. Under a bare nested server (no WM) it still maps fine and
    # focus_force below gives it focus.
    root.geometry(GEOMETRY)
    root.configure(bg="#f4f4f8")

    status = tk.Label(root, text="Ready", bg="#222", fg="#0f0",
                      font=("TkDefaultFont", 12), anchor="w", padx=10)
    status.pack(fill="x")

    def record(label: str) -> None:
        state["last"] = label
        if label == "Increment":
            state["count"] += 1
        elif label == "Reset":
            state["count"] = 0
            state["typed"] = ""
            entry.delete(0, "end")
        status.config(text=f"{label}  (count={state['count']})")
        persist()

    widgets = {}
    for label, color in (("Click Me", "#3060c0"), ("Increment", "#2a9d4a"), ("Reset", "#b03030")):
        button = tk.Button(root, text=label, bg=color, fg="white", activebackground=color,
                           font=("TkDefaultFont", 14, "bold"), height=2,
                           command=lambda lbl=label: record(lbl))
        button.pack(fill="x", padx=16, pady=8)
        widgets[label] = button

    tk.Label(root, text="Enter text:", bg="#f4f4f8", anchor="w").pack(fill="x", padx=16)
    entry = tk.Entry(root, font=("TkDefaultFont", 14))
    entry.pack(fill="x", padx=16, pady=(0, 8), ipady=6)
    widgets["Enter text"] = entry

    # Grab X keyboard focus ourselves: under a bare X server (no window manager, as in
    # the test sandboxes) a click doesn't assign keyboard focus, so typed keys would go
    # nowhere. Real apps can force focus; doing so makes typing land with or without a WM.
    entry.bind("<Button-1>", lambda _e: entry.focus_force())

    root.attributes("-topmost", True)  # stay above other windows so clicks reliably land

    def capture_geometry() -> None:
        """Record each widget's on-screen bbox so a test can click exact coordinates
        (and verify the backend→app path) without a VLM."""
        root.update_idletasks()
        root.lift()
        root.focus_force()
        state["widgets"] = {
            name: [w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()]
            for name, w in widgets.items()
        }
        persist()

    root.after(300, capture_geometry)

    def on_type(_event: "tk.Event") -> None:
        state["typed"] = entry.get()
        state["last"] = "type"
        status.config(text=f"typed: {entry.get()!r}")
        persist()

    entry.bind("<KeyRelease>", on_type)

    persist()
    root.mainloop()


if __name__ == "__main__":
    main()
