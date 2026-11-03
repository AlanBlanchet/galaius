# Changelog — Galaius (VS Code)

The extension version tracks the [galaius](https://github.com/AlanBlanchet/galaius) server
version. See the [full changelog](https://github.com/AlanBlanchet/galaius/blob/main/CHANGELOG.md).

## 0.43.0 — 2026-09-14

- Choose Best available when starting an agent; criteria rank models across providers while existing workspace permissions are preserved.
- Show one ordered model ranking with availability checked when the agent starts.

## [Unreleased]

### Added

- **Dashboard hot-reload** — refresh the Galaius panel without reloading the VS Code window.
- **Benchmarks tab** grouped by Image / GUI grounding / Video, each with a task description and a
  best-model ranking from public leaderboards.
- **Config sub-panels** (models, API keys, browser, desktop, benchmark data) and a **display
  currency** setting (live ECB conversion).

### Changed

- The zero-config server launch now runs galaius from GitHub via `uvx`, fixing startup when
  there's no local checkout.

## [0.1.0] — 2026-06-03

- Initial release: launches the galaius MCP server and provides a dashboard + settings UI.
