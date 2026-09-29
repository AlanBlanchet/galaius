# Host desktop: keys, monitors, regions and waits on the user's own screen

Why: an agent opened the OS launcher, typed a query, grabbed ONE monitor and waited for results
with raw `xdotool key super`, `xdotool type`, `convert -crop … -resize` and `sleep 4`. Each of
those is a capability galaius must own, on Linux, Windows and macOS alike.

## One interface, three implementations

`DesktopBackend` (backend.py) is the one interface. A `target="screen"` / `"screen:<n>"` is a
`DesktopWindow` bound to `host_desktop()` (host.py), so capture and input for the whole desktop go
through the same methods on every OS:

| method | Linux X11 `X11Display` (x11.py) | Windows `WindowsDesktop` | macOS `MacDesktop` |
|---|---|---|---|
| `key(chord)` / `type_text` | xdotool (XTEST), `--clearmodifiers`; unknown key name raises | pynput → `SendInput` | pynput → Quartz `CGEventPost` |
| `move` / `click` / `scroll` | xdotool | pynput → `SendInput` | pynput → `CGEvent` |
| `capture()` / `capture_region` | maim (`-g WxH+X+Y`) | mss → GDI `BitBlt` | mss → `CGWindowListCreateImage`, scaled to points |
| `monitors()` | `xrandr --listmonitors` | mss monitor list | mss monitor list |
| `windows()` | python-xlib: `_NET_CLIENT_LIST`, else root children; viewable only | `EnumWindows` (visible, not minimised, titled, not DWM-cloaked) | `CGWindowListCopyWindowInfo` (layer 0, on screen) |

`NestedBackend` (the sandbox) now IS an `X11Display` that owns its X server: one X11
implementation drives both the user's screen and the sandbox. Its `windows()` reuses the xdotool
enumeration launch_app polls, so a wait and a launch agree on what is open.

Coordinates: one image pixel = one input unit on every OS. macOS Retina captures come back at 2x;
`PortableBackend.capture_region` resizes them to the monitor's point size so a click at image
(x, y) lands on that pixel. Windows: mss makes the process per-monitor DPI aware, so capture and
`SendInput` both speak physical pixels.

## Capabilities: CURRENT → TARGET

(a) OS-level keys and typing aimed at the desktop itself

- CURRENT Linux: `run_actions(target="screen")` `key_press` / `type_text` went through raw xdotool
  inside `DesktopWindow`; a lone `cmd` / `win` / `meta` became an unknown key xdotool ignored while
  exiting 0; the MCP instructions said desktop was Linux-only and never mentioned keys on `screen`.
- CURRENT Windows / macOS: `PortableBackend.key` worked but `pageup` / `pagedown` (sent as the X
  names `Prior` / `Next`) raised.
- TARGET all three: `run_actions(target="screen", actions=[{"type":"key_press","key":"super"},
  {"type":"type_text","text":"whispering"}, {"type":"key_press","key":"Escape"}])`. `super`,
  `cmd`, `win`, `meta` all mean the OS key (Windows key, ⌘); the launcher is `super` on Windows
  and most Linux desktops, `cmd+space` on macOS. Chords: `alt+tab`, `ctrl+shift+t`.

(b) Screenshot of one monitor, a region, downscaled

- CURRENT Linux: `screen:<n>` crops one monitor; no region, no downscale. Windows / macOS: whole
  virtual screen only, `list_desktop_windows` printed one size.
- TARGET all three: `list_desktop_windows` lists monitors with geometry (and windows) on every OS;
  `screenshot(target="screen:1", region=[x, y, w, h], max_width=1280)` crops then downscales, and
  the reply states the scale so image coordinates map back to input coordinates. The same two
  parameters work on browser, window, nested and `file:` targets. On Windows / macOS
  `list_desktop_windows` prints each window as a ready `region=` for `target="screen"` (shifted
  by the capture origin, which is negative when a monitor sits left of the primary).

(c) Waiting on a condition instead of sleep

- CURRENT: on a desktop target `wait_for` was only a fixed pause.
- TARGET all three: `wait_for` takes `window="<title substring>"` (`state="visible"` appears,
  `"hidden"` gone), polled until `timeout` ms, failing with the windows it saw.
- NEXT (own review rounds): `screen="changes"` / `"settles"` — wait on the pixels. A first version
  failed review: real carets blink 530-600 ms, longer than the 250 ms poll, so a blink passed as a
  change (1 false change in 5 idle runs, Return missed 2 in 7). The rebuild ignores any cell that
  returns to how it looked before and requires a change to hold ~0.8 s.

## Not covered (stated)

- Wayland sessions: `host_desktop()` refuses with a message. uinput input (`LocalBackend`) reaches
  Wayland clients (see `.github/research/wayland-uinput-injection.md`), but capture needs the
  xdg-desktop-portal Screenshot API (GNOME / KDE) or `grim` (wlroots); neither is wired.
- Per-window targets (`target="<title>"`) on Windows / macOS: windows are listed and waited on,
  not captured or driven one by one; use `screen:<n>` + `region`.
- Recording `target="screen"` is Linux-only (ffmpeg x11grab).
- macOS needs Screen Recording (capture, window titles) and Accessibility (input) granted to the
  host app; without them titles come back empty and input is dropped by the OS.
- Retina scaling is written against mss's documented behaviour; CI macOS runners have scale 1,
  so the 2x path is unit-tested only.
