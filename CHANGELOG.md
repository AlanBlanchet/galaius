# Changelog

Notable changes to **interact**. Follows [Semantic Versioning](https://semver.org) and
[Keep a Changelog](https://keepachangelog.com). Releases are cut from `main` (and `release/X.Y`
maintenance branches) — see [RELEASING.md](RELEASING.md).

## [Unreleased]

## [0.4.1] — 2026-06-20

### Fixed

- **A browser `ref` now survives between tool calls** (#34). A `ref` handed out by
  `get_interactive_elements` / `get_page_state` / `screenshot` failed with "Element N not found"
  the moment it was used in a *separate* later `run_actions` call — even with no navigation or
  re-render. Cause: a tab-less scan registered the element map under key `None` while `run_actions`
  read it under the hardcoded tab `0` (`None != 0`), so the lookup always missed. The element map
  now keys on the active tab consistently, `run_actions` starts on the active tab, and a ref also
  resolves directly against the live `data-interact-ref` attribute (which persists across calls) —
  so a ref clicks even if the server-side map was cleared. Refs are now a first-class cross-call
  handle, no re-annotate-every-call workaround needed.

### Added

- **`target="screen"` desktop automation now works on macOS and Windows** (#24). The PortableBackend
  is wired through the MCP tool surface: `screenshot`, `run_actions`, `get_interactive_elements`
  with `target="screen"` capture + drive the real desktop on macOS/Windows (not just Linux).
  `list_desktop_windows` reports the screen target there; window-title targets and the `launch_app`
  nested sandbox stay Linux-only with a clear, actionable message. The whole path is verified on the
  **macos-latest + windows-latest CI runners** — capture, pointer round-trip, and the
  `target="screen"` tool resolution all pass on the real GUI sessions (3/3 on both, and a dedicated
  CI step shows the per-test result). The cross-platform goal: browser everywhere, plus real
  screen-level desktop automation everywhere.

## [0.3.6] — 2026-06-18

### Added

- **Cross-platform desktop backend for macOS/Windows** (#24). `PortableBackend` drives the real
  desktop via `mss` (screen capture) + `pynput` (pointer/keyboard) — OS-native underneath (Quartz /
  Win32 / Xlib) — so `target="screen"` automation has a real backend on macOS and Windows, not just
  Linux. `select_desktop_backend` picks it for `local` off Linux (Linux keeps the deeper
  uinput/X11 `LocalBackend`; the `nested` Xephyr sandbox stays Linux-only).
- **The macOS/Windows path is now tested on real runners.** `tests/test_portable_backend.py`
  exercises capture + pointer round-trip on the **macos-latest and windows-latest** CI matrix (the
  genuine GUI sessions), self-skipping on Linux and skipping-with-a-diagnostic when a host denies
  Screen-Recording/Accessibility — so there's a reproducible mac/win desktop test, not an
  unverifiable claim. (MCP-tool exposure of mac/win desktop targets follows once CI confirms the
  backend on those runners.)

## [0.3.5] — 2026-06-18

### Fixed

- **The nested sandbox no longer dies under concurrent interact servers** (#33). The Xephyr/Xvfb
  display number was hardcoded to `:99`, so several MCP servers (a user can have many) fought over
  it and the loser's display died seconds after `launch_app`, taking the launched app's windows
  with it. The sandbox now starts on the first FREE display (probing the X lock/socket) and retries
  the next free one on a startup race, reaping a failed server so no `<defunct>` Xephyr lingers.
  When a launch still maps no window, `launch_app` reports the X server's health/exit output instead
  of only the generic Qt-helper targets. Verified: two concurrent sandboxes now take distinct live
  displays.

### Added

- **`double_click` and `select_text` actions** (#32). `double_click` selects a word / fires a real
  dblclick (two `click`s don't coalesce); `select_text` makes a real DOM Selection in an element —
  so a selection-gated control (a Lexical/Payload inline toolbar, a colour swatch) can be actuated,
  which `drag` (HTML5 drag-and-drop) couldn't. Verified live in a contenteditable.

## [0.3.4] — 2026-06-18

### Fixed

- **Qt/menu popups are captured** (#31). A `QComboBox` drop-down (and any menu/tooltip) opens as a
  separate override-redirect window that a per-window `maim -i` never included, so the capture
  showed the control still collapsed. The nested capture now composites mapped override-redirect
  windows overlapping the target (a screen-region grab anchored at the window origin). Verified live
  driving a real PySide6 `QComboBox` in the sandbox.
- **Flutter blurred bars no longer render as a black strip** (#28). A Flutter `BackdropFilter`
  blur (e.g. a `ConvexAppBar` bottom nav) composites to solid black under the sandbox's software GL;
  `launch_app` now detects a Flutter Linux bundle and adds `--enable-software-rendering` (Skia CPU
  raster, no GL), so the bar renders and is tappable. Verified launching the real bundle.
- **Capture/input prefer the rendered window** (#28, #1). When a title matches several top-levels —
  a Flutter app exposes both its app-id window (`com.example.aino`) and the titled one (`aino`),
  one of which can be a transient black helper — window resolution now ranks a rendered window
  above a black one, so capture and clicks never land on the unrendered helper.

### Verified

- **Sandbox Chrome `--app` keyboard input** (#25) confirmed end-to-end on a multi-field login form
  (click field → type; each field receives its text) — the focus-by-resolved-wid + `--sync` fix
  from 0.3.2. Opt-in e2e reproductions added under `tests/test_sandbox_e2e.py`
  (`INTERACT_LOCAL_E2E=1`).

## [0.3.3] — 2026-06-18

### Fixed

- **`data-interact-ref` is unique per snapshot** (#29). A re-render could leave a stale ref on a
  surviving node while a new node got the same `eN`, so a click-by-ref hit Playwright's strict-mode
  "resolved to 2 elements". The DOM scan now clears all prior refs first, guaranteeing uniqueness.
- **A multi-match selector click targets the first VISIBLE element** (#29). `:has-text('Annuler')`
  / duplicated link text (breadcrumb mirroring the sidebar) used to click whatever was first in DOM
  order — often a hidden one, so the click silently landed wrong. It now picks the first visible
  match, and a genuinely ambiguous locator returns an actionable "narrow it / use a ref" message
  instead of Playwright's strict-mode dump.
- **Tab-less captures follow the active tab** (#30). After `new_tab` / `switch_tab`, a standalone
  `screenshot` / `get_page_state` / `get_interactive_elements` captured tab 0 instead of the tab the
  agent switched to. The session now tracks an active tab that those tools default to.

### Documentation

- Documented two software-GL/X11 sandbox capture limits and their workarounds (#28, #31): a Flutter
  blurred bar can render as a black strip under software GL, and Qt/menu popups open as separate
  override-redirect windows — capture the whole sandbox screen (`target="nested"`) or drive by
  keyboard. Both tracked under #1.

## [0.3.2] — 2026-06-16

### Fixed

- **Sandbox keyboard input focuses the exact target window** (#25). On the WM-less nested display,
  keyboard focus re-searched by title every keystroke (and without `--sync`), so it could land on a
  hidden helper window (Chrome spawns a 10×10 "clipboard" window) while clicks correctly used the
  resolved window — "clicks work, typing doesn't". Keyboard input now focuses the window's resolved
  `wid` (the same one click/scroll act on) with `--sync`. Verified driving a real Chrome `--app`
  window in a nested display (text + key presses land in the focused field).
- **Inline `screenshot` actions honour `path`** (#27). A `screenshot` action inside `run_actions`
  silently dropped its `path` (the field didn't exist), so the PNG was never written and agents had
  to make a separate standalone call that re-captured a now-changed page. The inline action now
  writes `path` the same way the standalone screenshot tool does (browser and desktop targets).

## [0.3.1] — 2026-06-16

### Fixed

- **`interact doctor` / `interact status` no longer print Linux-only advice on macOS/Windows.** They
  used to report `maim=MISSING (apt install maim)` and `/dev/uinput NOT writable — add a udev rule`
  on any OS, which reads as "broken" to a colleague whose browser automation is actually ready. Off
  Linux they now report desktop automation as not-available-here and point at the browser path.

## [0.3.0] — 2026-06-16

### Added

- **Browser automation is now first-class on macOS and Windows.** The MCP server boots on every OS
  and Playwright drives the browser there fully; the OS-agnostic suite runs on the ubuntu/macos/
  windows CI matrix to keep that guarantee. Native desktop control stays Linux/X11, but off Linux
  the desktop tools (`launch_app`, `target=<window>/screen/nested`, `list_desktop_windows`) now
  return **one actionable message** steering to the browser target instead of leaking a low-level
  `evdev`/`maim` error. Tracked for native backends: #24.
- **`emulate_device` action** (#21) — set a browser session to a device profile (a Playwright
  device name like `"iPhone 13"`, or explicit `width`+`height` with optional `device_scale_factor`/
  `is_mobile`/`has_touch`/`user_agent`; `reset=true` restores the default) to verify responsive and
  mobile layouts at true device metrics. Rebuilds the session context, preserving cookies + URL.

### Fixed

- **`evaluate_js` now returns its value** (#22, #23). A function-bodied script — `() => { …; return
  x }`, the shape every `getBoundingClientRect`/`getComputedStyle` read uses — was wrapped in a
  second IIFE that defined the arrow without calling it, so `page.evaluate` returned `undefined`.
  Function scripts now pass through untouched, and the return value is surfaced JSON-serialised as
  the step's primary output (not buried under a change description).
- **Nested-window capture survives a stale window id.** A multi-process app (Chrome) recreates its
  top-level window, so `maim -i <id>` could fail with a non-zero exit between enumeration and
  capture; it now re-resolves the title and retries, falling back to a full nested-screen grab
  rather than erroring.

## [0.2.5] — 2026-06-16

### Changed

- **The VS Code extension no longer silently runs a local checkout.** It used to auto-detect any
  workspace folder whose `pyproject.toml` is interact's and run *that working tree* via `uv run` —
  so a colleague who merely opened a clone got an unbuilt dev version. Dev mode is now **opt-in
  only** (the `interact.projectPath` setting or the `INTERACT_PROJECT_PATH` env var); by default
  everyone gets the published build via `uvx`, **pinned to the extension's own release tag** so the
  server matches the extension version — never `main`/dev HEAD. New `.env.example` documents this
  and the other `INTERACT_*` overrides (the home for anything machine-specific, kept out of the
  codebase).

### Fixed

- **CI is green across the matrix again** (it had been red on macOS/Windows since v0.2.0, which
  blocked every release tag and the Marketplace publish). The sandbox lifecycle tests spawned
  `sh`/`sleep` and opened `/dev/null` — POSIX-only — so they errored on Windows; they now spawn the
  Python interpreter and use `os.devnull`, so the cross-OS test job passes and the tag/release job
  can run.

### Added

- `npm run package` / `npm run publish` in the extension (build an `interact-<version>.vsix` or push
  to the Marketplace), and a README section clarifying the two VS Code paths (`interact install
  vscode` for just the tools, or the extension for the dashboard UI).

## [0.2.4] — 2026-06-15

### Fixed

- **`record()` on a sandbox window no longer returns all-black frames.** Video capture hardcoded
  ffmpeg `x11grab` on `:0` (the real display), so recording a `nested:` window grabbed the wrong
  display while `screenshot()` worked; it now records the window's own nested display. Verified live
  (non-black frames) ([#18](https://github.com/AlanBlanchet/interact/issues/18)).
- **A screenshot no longer lists element refs from a screen that's no longer shown.** Cached
  interactive-element refs are now surfaced only when the live frame's content signature matches the
  frame they were detected on — after a navigation the stale refs are withheld, so a click can't
  land on a gone target ([#19](https://github.com/AlanBlanchet/interact/issues/19)). A query
  screenshot also writes its file *after* the VLM call (in a `finally`), so the saved frame is
  exactly the one analysed, even on a VLM error ([#17](https://github.com/AlanBlanchet/interact/issues/17)).
- **Scroll and drag now reach a Flutter/GTK window.** Scroll raises + focuses the window before
  emitting wheel events (an unfocused toolkit silently drops them), and drag uses a fine,
  float-interpolated, time-spread pointer path instead of a couple of pixel-quantized teleports — so
  the toolkit recognises a drag/fling. Applies to both the real-desktop and sandbox paths
  ([#12](https://github.com/AlanBlanchet/interact/issues/12),
  [#13](https://github.com/AlanBlanchet/interact/issues/13)).
- **Sandbox zombie reaping is more aggressive.** Exited apps are now reaped on every capture, not
  only on the next `launch_app`, so a long session can't accumulate `<defunct>` children between
  launches (with the v0.2.2 dead-display respawn, closes the rc=1 root cause)
  ([#11](https://github.com/AlanBlanchet/interact/issues/11)).
- **The repaint heuristic handles a blur-backed bottom bar better.** The black-strip detector scans
  a band of candidate strip heights (a `ConvexAppBar` is taller than a plain nav bar), the repaint
  nudge resizes by a larger delta (enough to make Skia rebind a blurred layer, not just relayout),
  and up to two nudges are tried before a window is left alone — re-arming once it renders so a later
  navigation that goes black is nudged again ([#14](https://github.com/AlanBlanchet/interact/issues/14),
  [#15](https://github.com/AlanBlanchet/interact/issues/15),
  [#16](https://github.com/AlanBlanchet/interact/issues/16),
  [#20](https://github.com/AlanBlanchet/interact/issues/20)). A real compositor renders this class
  natively; a sway-headless backend is tracked as the durable fix
  ([#1](https://github.com/AlanBlanchet/interact/issues/1)).

## [0.2.3] — 2026-06-13

### Changed

- **The sandbox forces software GL, so `launch_app` needs no env tricks.** A nested Xephyr/Xvfb
  display has no usable hardware GL, so a GPU app that tries hardware EGL hit
  `DRI2: failed to create any config` and rendered black — agents had to know to prefix
  `env LIBGL_ALWAYS_SOFTWARE=1`. The sandbox now sets that itself (overridable), so
  `launch_app("<binary>")` renders out of the box. Verified driving aino with a bare command.

## [0.2.2] — 2026-06-13

### Fixed

- **The sandbox recovers when its display dies mid-session.** A long session can exhaust or kill the
  nested X server (e.g. dozens of GPU apps left running across rebuilds); the cached backend was
  reused regardless, so *every* later `launch_app` — even `xterm` — returned `rc=1` with no way to
  reset. Now a dead/unresponsive display is detected and **respawned automatically** on the next
  `launch_app` (or `screenshot`/`run_actions` against `nested:`), exited apps are reaped so they
  don't pile up, and a `reset_sandbox` tool force-clears everything on demand. A genuine app crash
  now surfaces the app's **own stderr** in the message, so "the app failed" reads differently from
  "the display died" ([#10](https://github.com/AlanBlanchet/interact/issues/10)).

## [0.2.1] — 2026-06-13

### Fixed

- **Nested sandbox: a Flutter/GL window no longer captures black.** Under software GL
  (`LIBGL_ALWAYS_SOFTWARE=1`) a Flutter/Electron window can present a stale, uninitialised buffer to
  X — the whole frame, or its blurred `BottomNavigationBar`, comes back solid black, hiding (and
  making untappable) the nav. `launch_app` now nudges each new window once (a 2px resize) to force a
  full repaint, which then persists; and capture self-heals — a frame that looks unrendered triggers
  one repaint + recapture. Verified live driving aino's GPU UI in the sandbox
  ([#7](https://github.com/AlanBlanchet/interact/issues/7),
  [#8](https://github.com/AlanBlanchet/interact/issues/8)). A genuinely pure-black/OLED UI (where
  the repaint changes nothing) is nudged at most once, then left alone, so its scroll isn't reset on
  every capture ([#9](https://github.com/AlanBlanchet/interact/issues/9)).
- **Keyboard input now reaches a sandbox window.** The sandbox has no window manager, so nothing
  held the X input focus and typed text/keys went nowhere; `type`/`key` now focus the target first
  via `windowfocus` (XSetInputFocus), which works WM-less — unlike `windowactivate`, which needs
  `_NET_ACTIVE_WINDOW`, the very error that drove a consumer to drive windows by hand
  ([#6](https://github.com/AlanBlanchet/interact/issues/6)).

## [0.2.0] — 2026-06-13

### Added

- **Publishing pipeline** — CI publishes the VS Code extension to the Marketplace and Open VSX on
  each new release tag (each self-skips until its token is configured); the CLI installs from
  GitHub via the one-liner or `uv` on macOS, Linux, and Windows.
- **`report_issue` tool** — agents (and users) can send a bug / limitation / feedback about
  interact itself; it opens a GitHub issue (or saves locally as a fallback). The server tells
  connecting clients this exists.
- **`target="screen"` / `screen:<n>`** — capture and act on the whole virtual desktop or a single
  monitor (enumerated via `list_desktop_windows`), with correct multi-monitor input mapping.
- **Per-step interaction video** — `run_actions(record=True)` captures a frame per step so a video
  model can explain what happened, sampled to a cost bound.
- **`navigate` accepts a `timeout`** (ms) for slow dev servers that compile routes on first hit —
  e.g. `timeout=60000` for a cold Next.js route ([#4](https://github.com/AlanBlanchet/interact/issues/4)).
- **Target a window by id** — `target="wid:<id>"` (decimal or `0x` hex, shown by
  `list_desktop_windows`) selects exactly when no title is unique ([#5](https://github.com/AlanBlanchet/interact/issues/5)).
- **Headless / sandboxed app driving** — `launch_app("<cmd>")` runs an app in an isolated display
  the agent owns (Xephyr, or headless Xvfb), then drive it with `target="nested:<title>"` /
  `target="nested"`. Non-intrusive (never touches the user's windows, cursor, or focus) and
  occlusion-proof — for apps that fight the WM or won't screen-grab on the real desktop ([#1](https://github.com/AlanBlanchet/interact/issues/1)).
- **The server steers agents to the sandbox and reports its version.** Connection instructions now
  tell agents to drive native apps with `launch_app` (never shell out to xdotool) when a window
  fights the WM or screen-grabs black, and carry `interact vX.Y.Z` — so a stale server (one missing
  the newer tools) is obvious and the fix is "reconnect," not "reimplement with raw shell."

### Changed

- **Window targeting prefers an exact title** and refuses to silently guess: several partial
  matches with no exact one return the candidate list so the agent can disambiguate (`"aino"` vs
  `"aino - Visual Studio Code"`) ([#1](https://github.com/AlanBlanchet/interact/issues/1)).
- **`list_desktop_windows` offers connector-name screen targets** (`screen:DP-1`) — stable across
  sessions, unlike indices that reorder on display-manager restart ([#1](https://github.com/AlanBlanchet/interact/issues/1)).

- **Grounding strategy is derived from live model capabilities** (computer-use / GUI-grounding /
  video, read from the model catalog) instead of being hardcoded — native-coordinate models like
  Opus get coordinate grounding, others get a ref list.
- **Shared settings schema** drives both the CLI/TUI config and the VS Code settings UI from one
  source, fixing env-var drift between them.
- **Benchmarks** are categorized (Image / GUI grounding / Video) with a task description and
  best-model ranking sourced from public leaderboards, cached locally with a timestamp.

### Fixed

- **`get_interactive_elements` no longer piles stale annotations from earlier screens onto the
  current one.** The per-window ref cache keyed off the window *title* — constant in single-window
  apps (Flutter/Electron/games) — so it never reset and unioned every detection (8→28 boxes across
  one session, refs pointing at gone elements). It now keys off a screenshot content fingerprint, so
  navigating to a new screen discards the old refs.
- **Sandbox targeting picks the real window.** `target="nested:<title>"` now selects the largest
  visible window of that title — toolkits (Flutter/GTK) map a hidden same-titled helper window, and
  the phantom would win; `list_windows` also reports a just-mapped window so `launch_app`'s poll
  detects an app mid-startup. Verified driving aino's GPU UI in the sandbox.
- **A moved or backgrounded window is now interactable.** Capture/record raise + activate the
  target window first, so it yields its own pixels instead of whatever buries it — the gap that
  drove a consumer to run `xdotool windowactivate` by hand ([#1](https://github.com/AlanBlanchet/interact/issues/1)).
- **The client-log scan reports its coverage** and takes multiple roots (`--projects-root`
  repeatable), so the dogfood loop can't silently miss a surface a consumer runs from.
- **VS Code usage panel now live-syncs.** It watches the usage log — which the MCP server writes
  from a separate process — and refreshes instead of freezing at open. It also reads the log under
  the configured `interact.debug.dir` instead of a hardcoded `~/.interact`, and that setting now
  maps to the correct `INTERACT_DEBUG_DIR` (it was wired to the screenshot-dump dir and kept in a
  duplicate env map that defeated the shared settings schema; the extension now uses the schema).
- **`record(target="screen:N")` crashed** asking xdotool for the geometry of a synthetic screen
  window id; it now grabs the monitor's known region directly, like `screenshot`
  ([#3](https://github.com/AlanBlanchet/interact/issues/3)).
- **VS Code zero-config launch** — the extension now runs interact from GitHub via `uvx` instead of
  failing to resolve it (the bare `interact` name on PyPI is an unrelated package).
- **GPU-surface capture** (Android emulator, games, hardware-accelerated video) now reports a clear
  diagnostic instead of returning a black image.

## [0.1.0] — 2026-06-03

- Initial release: MCP server for browser **and** desktop automation with optional VLM analysis, a
  unified `interact` CLI + config TUI, and a VS Code extension — usable from any MCP client.

[Unreleased]: https://github.com/AlanBlanchet/interact/compare/v0.2.5...HEAD
[0.2.5]: https://github.com/AlanBlanchet/interact/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/AlanBlanchet/interact/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/AlanBlanchet/interact/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/AlanBlanchet/interact/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/AlanBlanchet/interact/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/AlanBlanchet/interact/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AlanBlanchet/interact/releases/tag/v0.1.0
