# Changelog

Notable changes to **interact**. Follows [Semantic Versioning](https://semver.org) and
[Keep a Changelog](https://keepachangelog.com). Releases are cut from `main` (and `release/X.Y`
maintenance branches) — see [RELEASING.md](RELEASING.md).

## [Unreleased]

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

[Unreleased]: https://github.com/AlanBlanchet/interact/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AlanBlanchet/interact/releases/tag/v0.1.0
