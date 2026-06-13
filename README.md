# interact

**Browser _and_ desktop automation for AI agents — over MCP.**

[![CI](https://github.com/AlanBlanchet/interact/actions/workflows/ci.yml/badge.svg)](https://github.com/AlanBlanchet/interact/actions/workflows/ci.yml)
[![VS Code Marketplace](https://img.shields.io/visual-studio-marketplace/v/AlanBlanchet.interact?label=VS%20Code)](https://marketplace.visualstudio.com/items?itemName=AlanBlanchet.interact)
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
# macOS / Linux — one-liner (installs uv if missing, then the global `interact` CLI)
curl -LsSf https://raw.githubusercontent.com/AlanBlanchet/interact/main/install.sh | sh

# any platform, including Windows — with uv
uv tool install git+https://github.com/AlanBlanchet/interact
uvx --from git+https://github.com/AlanBlanchet/interact interact mcp   # run without installing
```

> Installs the **`interact`** command from GitHub (`uv` makes it one command on macOS, Linux, and
> Windows). interact isn't on PyPI — the bare `interact` name is taken there.

**VS Code** — install the **Interact** extension for a dashboard + settings UI (it launches the
same server):
[Marketplace](https://marketplace.visualstudio.com/items?itemName=AlanBlanchet.interact) ·
[Open VSX](https://open-vsx.org/extension/AlanBlanchet/interact) (Cursor / Windsurf / VSCodium).

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

### Which model should I use?

interact uses a model for three distinct jobs. Set each, or leave it on **auto** (the safe
default — it resolves to the best available model among the providers you have keys for, cheapest
capable first, and falls back automatically if one errors):

| Role | What it does | Pick one that's good at… | Ranked by (Benchmarks tab) |
| --- | --- | --- | --- |
| **image** | reads a screenshot to answer a `query`; the default for most vision calls | general image understanding | **Image** (e.g. MMMU) |
| **component** | detects the clickable UI elements / grounds _where_ to click | **GUI grounding** | **GUI grounding** (ScreenSpot-Pro) |
| **video** | reads a recorded interaction's frames to explain what happened | temporal / sequence understanding | **Video** (Video-MME) |

How to choose, in order of effort:

- **Do nothing — use auto.** It already picks a capable, cheaper-first model per role for your keys.
- **Want the strongest for a job?** Open the dashboard's **Benchmarks** tab: it ranks current
  models per category (Image / GUI grounding / Video) from public leaderboards and shows the best
  ones — pick the top model your provider offers. Each benchmark explains the task it measures, so
  you know _why_ a model is "best" for that job. (Keep scores live by adding a source key under
  **Benchmark data**.)
- **Cost-conscious?** The dashboard and `interact providers` show cost per model; auto already
  prefers cheaper capable models, and `interact usage` tracks what you've spent.
- **The one that matters most is `component` (grounding).** Reliable clicking on dense desktop /
  canvas UIs needs a _grounding-capable_ model (one with the `gui_grounding` / `computer_use`
  capability — derived from the live catalog, shown in the panel); a general VLM mislocates.

`interact status` shows what each role currently resolves to. Change them in the TUI (`interact`),
the VS Code **Configuration → Models** panel, or the CLI (`interact config set <role>.model <id>`).

## What your agent can do

- **`navigate`** — open a URL; returns title + visible text (or a vision answer with `query`).
- **`run_actions`** — the workhorse: a batch of `click` / `type_text` / `scroll` / `drag` /
  `key_press` (+ `wait`/`observe` per step), each reporting what changed.
- **`screenshot`**, **`get_interactive_elements`**, **`get_page_state`** — see and inspect.
- **Desktop** — `list_desktop_windows`, and the same actions/screenshot against a window (by title
  or `wid:<id>`) or the whole screen. A moved or backgrounded window is raised before capture, so
  it's always interactable.
- **`launch_app`** — run an app in an isolated display the agent owns, then drive it with
  `target="nested:<title>"`. Non-intrusive (never touches your windows/cursor/focus) and
  occlusion-proof — the reliable path for apps that fight the window manager.
- **`report_issue`** — hit a bug or a missing capability in interact itself? Agents can file it
  straight to the maintainers: it becomes a GitHub issue (authed `gh`), or your browser opens the
  prefilled issue page — you just press Submit. Same channel from any shell:
  `interact report "title" "what happened" --kind bug|limitation|feedback`.

## Platform support

| | Linux | macOS | Windows |
| --- | :-: | :-: | :-: |
| Browser, MCP server, CLI, TUI | ✅ | ✅ | ✅ |
| Desktop control (real windows) | ✅ (X11; uinput input also on Wayland) | ⏳ | ⏳ |

Browser automation and everything else are cross-platform. Native desktop control is Linux/X11
today (with a nested Xephyr/Xvfb sandbox); other desktop paths error clearly rather than misbehave.

> **GPU-rendered windows** (Android emulator, games, hardware-accelerated video) can't be read by
> an X screen-grab without a compositor — capture comes back uniform black, and interact says so
> (rather than handing back a black image). Options: run the app via `launch_app` in the sandbox
> (often software-renders, so it captures), run a compositor like `picom`, or grab the app's own
> framebuffer (e.g. `adb exec-out screencap -p` for an Android emulator).

## Development

```bash
git clone https://github.com/AlanBlanchet/interact && cd interact
uv sync
uv run pytest -m "not integration"      # fast, cross-platform suite
uv tool install --force --editable .    # put your checkout's `interact` on PATH
```

CI runs the suite on Linux/macOS/Windows plus a sandboxed desktop job; on push to `main` it tags
and publishes the release automatically from `pyproject.toml`'s version (see [RELEASING.md](RELEASING.md)).

## Contributing

Issues and PRs welcome. Please add a failing test for a bug before fixing it, keep the suite
green (`uv run pytest -m "not integration"`), and note user-facing changes in
[CHANGELOG.md](CHANGELOG.md). Cutting a release is one command — see [RELEASING.md](RELEASING.md).

## License

[MIT](LICENSE) © Alan Blanchet
