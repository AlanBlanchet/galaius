# Changelog — Interact (VS Code)

The extension version tracks the [interact](https://github.com/AlanBlanchet/interact) server
version. See the [full changelog](https://github.com/AlanBlanchet/interact/blob/main/CHANGELOG.md).

## [Unreleased]

### Added

- **Dashboard hot-reload** — refresh the Interact panel without reloading the VS Code window.
- **Benchmarks tab** grouped by Image / GUI grounding / Video, each with a task description and a
  best-model ranking from public leaderboards.
- **Config sub-panels** (models, API keys, browser, desktop, benchmark data) and a **display
  currency** setting (live ECB conversion).

### Changed

- The zero-config server launch now runs interact from GitHub via `uvx`, fixing startup when
  there's no local checkout.

## [0.1.0] — 2026-06-03

- Initial release: launches the interact MCP server and provides a dashboard + settings UI.
