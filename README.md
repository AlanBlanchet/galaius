# interact

**Browser _and_ desktop automation for AI agents — over MCP.**

[![CI](https://github.com/AlanBlanchet/interact/actions/workflows/ci.yml/badge.svg)](https://github.com/AlanBlanchet/interact/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-black.svg)](https://modelcontextprotocol.io/)

Like Playwright — but your agent acts on **what's on screen** (vision grounding), drives
**real desktop windows** as well as a headless browser, plugs into **any MCP client**, and
gets back a **text summary of what changed** instead of raw screenshots (so it stays fast and
cheap).

```bash
curl -LsSf https://raw.githubusercontent.com/AlanBlanchet/interact/main/install.sh | sh
interact install claude     # or: cursor | vscode | codex | windsurf | zed | claude-desktop
```

That's it — your agent can now navigate, click, type, scroll, drag, and read back what
happened.

## Why interact

- **Vision grounding** — act by what's visible ("click Submit"), not just CSS selectors.
- **Browser + desktop** — a headless browser _and_ real OS windows, one API.
- **Any MCP client** — Claude Code, Cursor, VS Code/Copilot, Codex, Windsurf, Zed, Claude Desktop.
- **Text diffs, not screenshots** — each call returns what changed; vision analysis is opt-in via `query`.
- **One command** — CLI + config TUI + MCP server in a single `interact`.

## Install

```bash
# one-liner (installs uv if missing, then the global `interact` CLI)
curl -LsSf https://raw.githubusercontent.com/AlanBlanchet/interact/main/install.sh | sh

# or with uv directly
uv tool install git+https://github.com/AlanBlanchet/interact
uvx --from git+https://github.com/AlanBlanchet/interact interact mcp   # run without installing
```

VS Code users can also install the **Interact** extension (publisher `AlanBlanchet`) for a
dashboard + settings UI — it launches the same server.

## Connect it to your agent

```bash
interact install claude          # registers the MCP server with the client
interact install vscode          # global (uses VS Code's `code --add-mcp`)
interact doctor                  # check keys, providers, Playwright, desktop
```

Supported: **claude, cursor, codex, vscode/copilot, windsurf, zed, claude-desktop**.

## Configure (models, keys, usage)

Run `interact` with no arguments for a terminal UI to set models, manage API keys, see what
you're connected to, and view usage/cost:

```bash
interact                                  # configuration TUI
interact config set OPENAI_API_KEY sk-…   # or set keys/models from the CLI
interact status                           # bindings + models + keys + usage at a glance
interact usage                            # spend / tokens by model and provider
```

Settings and keys live in `~/.interact/config.env` and are picked up whenever a client
launches the server. API keys are read from the usual provider env vars
(`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `ZAI_API_KEY`, …).

## What your agent can do

- **`navigate`** — open a URL; returns title + visible text (or a vision answer with `query`).
- **`run_actions`** — the workhorse: a batch of `click` / `type_text` / `scroll` / `drag` /
  `key_press` (+ `wait`/`observe` per step), each reporting what changed.
- **`screenshot`**, **`get_interactive_elements`**, **`get_page_state`** — see and inspect.
- **Desktop** — `list_desktop_windows`, and the same actions/screenshot against a window or the
  whole screen, with an isolated nested-display sandbox for safe testing.

## Platform support

| | Linux | macOS | Windows |
| --- | :-: | :-: | :-: |
| Browser, MCP server, CLI, TUI | ✅ | ✅ | ✅ |
| Desktop control (real windows) | ✅ (X11; uinput input also on Wayland) | ⏳ | ⏳ |

Browser automation and everything else are cross-platform. Native desktop control is Linux/X11
today (with a nested Xephyr/Xvfb sandbox); other desktop paths error clearly rather than misbehave.

## Development

```bash
git clone https://github.com/AlanBlanchet/interact && cd interact
uv sync
uv run pytest -m "not integration"      # fast, cross-platform suite
uv tool install --force --editable .    # put your checkout's `interact` on PATH
```

CI runs the suite on Linux/macOS/Windows plus a sandboxed desktop job; releases are tagged
automatically from `pyproject.toml`'s version on push to `main`.

## Contributing

Issues and PRs welcome. Please add a failing test for a bug before fixing it, keep the suite
green (`uv run pytest -m "not integration"`), and bump the version with
`uv run python -m interact.versioning bump <patch|minor|major>` when your change ships.

## License

[MIT](LICENSE) © Alan Blanchet
