"""GTK test window for interactive MCP tool validation.

Includes: buttons, text entry, toggle, counter, drag & drop zone, status feedback.
Designed for testing get_interactive_elements → run_actions(ref-based) flow.

Test scenarios:
  1. Detect elements → verify buttons, entry, switch, drag zones found
  2. Click "Click Me" via ref → status shows "Clicked: Click Me"
  3. Type in entry via ref → echo label updates
  4. Toggle switch via ref → status shows "Switch: ON"
  5. Click +/- via ref → counter label updates
  6. Drag "Drag Me" to "Drop Here" via from_ref/to_ref → drop label shows "Dropped!"
  7. Click "Reset" → everything resets
"""

import os

os.environ.pop("DESKTOP_STARTUP_ID", None)
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk


class TestWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Interact Test")
        self.set_default_size(420, 480)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.add(root)

        # --- Header ---
        label = Gtk.Label()
        label.set_markup("<b>Interact MCP Test</b>")
        root.pack_start(label, False, False, 0)

        # --- Status bar (shows last action) ---
        self.status = Gtk.Label(label="Ready")
        self.status.set_halign(Gtk.Align.START)
        root.pack_start(self.status, False, False, 0)

        # --- Buttons row ---
        btn_box = Gtk.Box(spacing=6)
        root.pack_start(btn_box, False, False, 0)
        for name in ["Click Me", "Toggle", "Reset"]:
            btn = Gtk.Button(label=name)
            btn.connect("clicked", self._on_button)
            btn_box.pack_start(btn, True, True, 0)

        # --- Text entry ---
        entry_box = Gtk.Box(spacing=6)
        root.pack_start(entry_box, False, False, 0)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Type here...")
        self.entry.connect("changed", self._on_entry_changed)
        entry_box.pack_start(self.entry, True, True, 0)
        self.echo_label = Gtk.Label(label="")
        entry_box.pack_start(self.echo_label, False, False, 0)

        # --- Toggle switch ---
        toggle_box = Gtk.Box(spacing=6)
        root.pack_start(toggle_box, False, False, 0)
        toggle_label = Gtk.Label(label="Feature:")
        toggle_box.pack_start(toggle_label, False, False, 0)
        self.switch = Gtk.Switch()
        self.switch.connect("state-set", self._on_switch)
        toggle_box.pack_start(self.switch, False, False, 0)
        self.switch_status = Gtk.Label(label="OFF")
        toggle_box.pack_start(self.switch_status, False, False, 0)

        # --- Counter ---
        counter_box = Gtk.Box(spacing=6)
        root.pack_start(counter_box, False, False, 0)
        self.counter = 0
        self.counter_label = Gtk.Label(label="Count: 0")
        counter_box.pack_start(self.counter_label, False, False, 0)
        inc_btn = Gtk.Button(label="+")
        inc_btn.connect("clicked", self._on_increment)
        counter_box.pack_start(inc_btn, False, False, 0)
        dec_btn = Gtk.Button(label="-")
        dec_btn.connect("clicked", self._on_decrement)
        counter_box.pack_start(dec_btn, False, False, 0)

        # --- Drag & Drop zone ---
        sep = Gtk.Separator()
        root.pack_start(sep, False, False, 4)

        dnd_label = Gtk.Label()
        dnd_label.set_markup("<small>Drag &amp; Drop</small>")
        dnd_label.set_halign(Gtk.Align.START)
        root.pack_start(dnd_label, False, False, 0)

        dnd_box = Gtk.Box(spacing=12)
        root.pack_start(dnd_box, False, False, 0)

        # Source: draggable item
        self.drag_source = Gtk.EventBox()
        self.drag_source.set_size_request(80, 60)
        source_label = Gtk.Label(label="Drag Me")
        self.drag_source.add(source_label)
        self.drag_source.override_background_color(
            Gtk.StateFlags.NORMAL, Gdk.RGBA(0.6, 0.8, 1.0, 1.0)
        )
        self.drag_source.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK,
            [Gtk.TargetEntry.new("text/plain", 0, 0)],
            Gdk.DragAction.MOVE,
        )
        self.drag_source.connect("drag-data-get", self._on_drag_data_get)
        dnd_box.pack_start(self.drag_source, False, False, 0)

        # Target: drop zone
        self.drop_target = Gtk.EventBox()
        self.drop_target.set_size_request(120, 60)
        self.drop_label = Gtk.Label(label="Drop Here")
        self.drop_target.add(self.drop_label)
        self.drop_target.override_background_color(
            Gtk.StateFlags.NORMAL, Gdk.RGBA(0.9, 0.9, 0.7, 1.0)
        )
        self.drop_target.drag_dest_set(
            Gtk.DestDefaults.ALL,
            [Gtk.TargetEntry.new("text/plain", 0, 0)],
            Gdk.DragAction.MOVE,
        )
        self.drop_target.connect("drag-data-received", self._on_drag_data_received)
        dnd_box.pack_start(self.drop_target, True, True, 0)

        # --- Action log ---
        self.log_label = Gtk.Label(label="Log: (empty)")
        self.log_label.set_halign(Gtk.Align.START)
        self.log_label.set_line_wrap(True)
        root.pack_start(self.log_label, False, False, 4)

    def _set_status(self, text: str):
        self.status.set_text(text)
        self.log_label.set_text(f"Log: {text}")

    def _on_button(self, btn):
        name = btn.get_label()
        if name == "Reset":
            self.counter = 0
            self.counter_label.set_text("Count: 0")
            self.entry.set_text("")
            self.switch.set_active(False)
            self.drop_label.set_text("Drop Here")
            self._set_status("Reset done")
        elif name == "Toggle":
            self.switch.set_active(not self.switch.get_active())
        else:
            self._set_status(f"Clicked: {name}")

    def _on_entry_changed(self, entry):
        text = entry.get_text()
        self.echo_label.set_text(text[:20])
        self._set_status(f"Typed: {text[:30]}")

    def _on_switch(self, switch, state):
        self.switch_status.set_text("ON" if state else "OFF")
        self._set_status(f"Switch: {'ON' if state else 'OFF'}")

    def _on_increment(self, _):
        self.counter += 1
        self.counter_label.set_text(f"Count: {self.counter}")
        self._set_status(f"Count: {self.counter}")

    def _on_decrement(self, _):
        self.counter -= 1
        self.counter_label.set_text(f"Count: {self.counter}")
        self._set_status(f"Count: {self.counter}")

    def _on_drag_data_get(self, widget, context, data, info, time):
        data.set_text("dragged_item", -1)

    def _on_drag_data_received(self, widget, context, x, y, data, info, time):
        self.drop_label.set_text("Dropped!")
        self.drop_target.override_background_color(
            Gtk.StateFlags.NORMAL, Gdk.RGBA(0.6, 1.0, 0.6, 1.0)
        )
        self._set_status("Drop received!")


win = TestWindow()
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
