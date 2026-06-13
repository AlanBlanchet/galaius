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
