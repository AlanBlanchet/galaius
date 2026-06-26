# interact — contributor notes

`interact` is a browser **and** desktop automation MCP server with optional VLM (vision)
analysis, usable by any MCP client (Claude / Cursor / VS Code+Copilot / Codex / Windsurf /
Zed / Claude Desktop). One Python core in `src/interact/` drives three surfaces: the
**CLI**, the **MCP server** (`interact mcp`), and the **VS Code extension**
(`vscode-extension/`).

## Versioning — semver, batched

- `pyproject.toml` `version` is the source of truth; keep `vscode-extension/package.json`
  `version` in lock-step (`python -m interact.versioning bump <patch|minor|major>` bumps both;
  `... check` verifies they agree — the pre-commit hook and CI enforce it).
- Don't bump on every change. Bump once when cutting a release: **patch** = fix, **minor** =
  backward-compatible feature, **major** = breaking change.
- Tags are created by CI on push to `main` (never by hand). Editable installs cache the
  version — `uv tool install --force --editable .` to refresh `interact --version`.

## Architecture (where things live)

- `desktop_backend.py` — `DesktopBackend` ABC. `LocalBackend` = the real session: a uinput
  **absolute pointer** (`INPUT_PROP_DIRECT`, maps over the full X root across all monitors)
  plus a separate uinput keyboard, `maim` capture. `NestedBackend` = an isolated display the
  agent owns — **Xephyr** (visible) or **Xvfb** (headless). Pick via `select_desktop_backend`.
- `desktop.py::DesktopWindow` — routes input/capture through a bound `DesktopBackend` when set
  (the nested sandbox), else the real-display **xdotool** path. `frames.py` converts
  coordinate spaces (screen ↔ monitor ↔ window ↔ image).
- `cli.py` is for the user: bare `interact` → the config TUI; `install`/`status` (connectors),
  `config`, `usage`, `providers`, `doctor`, `update`, `mcp`. **Not** scenarios.
- `probe.py` (`DetectionProbe` / `Scenario` / `DesktopScenario`) is **test infrastructure**,
  driven from `tests/`, never a user CLI command.
- `tui.py` — the bare-`interact` config TUI; persists via `UserConfig` to
  `~/.interact/config.env`, the same store the CLI reads and the extension mirrors through
  `SETTING_ENV_MAP`.
- Data sources of truth: `PackageData` (bundled `models.json` / `benchmarks.json` /
  `published_scores.json`) and `~/.interact/logs/usage.jsonl` (VLM usage).

## Tool surface model (the MCP API agents see)

Generic actions are ONE tool selected by a `target` param — not a tool per surface — while
browser-only capabilities stay as their own clearly-named tools:

- **Generic** (`run_actions`, `screenshot`, `get_interactive_elements`, `record`, `review_ui`,
  `verify_ui`, `measure_ui`): `target` = unset/`"browser"` (default — the browser session named by `session`) |
  a window-title string (a native desktop window) | `"screen"` (whole virtual desktop) |
  `"screen:<index>"` or `"screen:<output>"` (one monitor, enumerated via `xrandr --listmonitors`,
  shown by `list_desktop_windows`) | `"file:<path>"` (analyze/measure an existing image instead of
  capturing — for `screenshot`/`review_ui`/`measure_ui`). A desktop `target` and a non-default
  `session` are mutually exclusive. Screen/monitor targets capture via `maim` geometry and map input
  by the region origin (so multi-monitor clicks land on the right screen). `review_ui` (VLM defect
  discovery), `verify_ui` (PASS/FAIL each literal requirement) and `measure_ui` (deterministic WCAG
  contrast / colour, NO VLM) are the discover → verify → measure trio; `transcribe` (audio → text +
  understanding) is file-based, not `target`-keyed.
- **Browser-only** (no desktop meaning, stay separate): `navigate`, tab control,
  `get_page_state`, network/console logs, sessions, `download_asset`, JS eval. `get_page_state`
  and no-query `screenshot` also return the page's `ref` list (pure DOM scan, no VLM) so the
  agent can act by `ref` without a separate `get_interactive_elements` call.
- **Returns**: a short prose summary first, optional structured trailer; errors prefixed
  `ERROR:` so an agent can branch.

## Issues first, then client logs (do this BEFORE each iteration — no exceptions)

**Step zero of every iteration: read the GitHub issues.** They are what agents and users
actively reported (via the `report_issue` MCP tool or `interact report`); each open issue is
a candidate work item — integrate what's actionable into the iteration, and when a fix ships,
comment + close the issue. Then scan the client logs: `interact` is consumed as an MCP server
by the user's other projects, and those clients (Claude Code) log every tool call + result
under `~/.claude/projects` — real usage fails in ways the tests don't.

```bash
gh issue list --repo AlanBlanchet/interact              # FIRST: active reports → work items
uv run python scripts/scan_client_errors.py            # then: last 24h errors, grouped
uv run python scripts/scan_client_errors.py --all      # whole-history taxonomy
```

Feedback always travels through the channel, never the tree: file problems with
`interact report "<title>" "<body>" --kind bug|limitation|feedback` (or the `report_issue`
MCP tool — same path). Without an authed gh it opens the prefilled issue page in the user's
browser (they press Submit); only with no browser either does it save to
`~/.interact/feedback/` + a submit link. Never hand-write report files into `.github/` or
elsewhere in the repo. If a fallback file shows up in `~/.interact/feedback/`, deliver it
(file the issue), then delete it.

**Innovative feature ideas go through the same channel — but only the pertinent ones.** When a
genuinely interesting capability surfaces (often while using interact to drive a real app and
hitting a gap), don't just mention it in chat — file it as `--kind feedback` so it persists as a
work item. First judge whether it's *pertinent for interact*, because the bar is fit, not novelty:
does it generalize across MCP clients (not a one-off for the app you happen to be driving); does
it sit cleanly in the tool-surface model (a `target` on an existing generic tool, or a clearly
browser-only tool — not a redundant near-duplicate); does it keep the core generic, clean, and
flexible for integrators; does the value earn its surface area? Yes on those → file it (a crisp
title + the use case + why it's general). No → drop it rather than spend an issue on it. When in
doubt, file as feedback and let triage decide; never spam the tracker with every passing thought.

Note: the editable install is live, so a reconnecting client can momentarily run half-edited
source — verify a surprising logged error against committed code before treating it as a bug.

## Testing

- Bug fix → write the failing test first.
- Desktop tests use the **nested** sandbox (isolated, free, non-intrusive). The local uinput
  path is system-wide and can't be isolated, so its rich e2e is opt-in (`INTERACT_LOCAL_E2E=1`).
- **Never spend on models in unit tests.** A conftest fixture blocks real `litellm` calls in
  non-`integration` tests, so any un-mocked VLM path fails fast instead of hanging/spending.
  Real-model tests are `integration`-marked and key-gated; `pytest-timeout` (thread method)
  turns any residual hang into a named failure.
- **Availability never calls litellm.** `Model.is_available()` is a pure env-key check —
  `litellm.validate_environment`/`acompletion` can trigger an interactive device-code auth
  flow (e.g. the `chatgpt` provider) that blocks forever; never auto-select a keyless provider.
- Reproduce CI locally with keys cleared + `DISPLAY=` before pushing; drive CI green.
- Tk fixtures: spawn with a Tk-capable Python (uv's standalone Tk aborts under XCB) and use
  normal WM-managed windows so click-to-focus works.

## Git

Direct commits to `main` with conventional messages, authored solely by the maintainer —
**no AI / co-author trailers** in commit messages or PRs. Stage files explicitly. Enable the
tracked hook once per clone: `git config core.hooksPath .githooks`.
