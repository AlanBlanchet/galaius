# interact

MCP server: browser + desktop automation with VLM vision.

## Architecture

- Python 3.11, uv, src layout `src/interact/`
- FastMCP protocol, Playwright browser, AT-SPI + VLM desktop, litellm VLM
- Dual-model VLM: primary model for analysis, non-thinking component model for element detection
- Structured output (Pydantic response_format) for element detection, fallback to text parsing
- Circuit breaker on VLM failures → skip failed model temporarily, fallback gracefully
- Provider-agnostic: no model-specific branching in generic code — all VLM output normalized to pixel-coordinate JSON, provider config lives in extension settings
- Data-driven CoordFormat: `_COORD_FORMATS` in generate-models.py → models.json `coordFormats` → extension injects via `INTERACT_MODELS_JSON` env var → server extracts `coordFormats` key and calls `load_formats()` at startup
- CoordFormat fields (divisor, prompt_template, box_order, box_key, normalized) fully configurable per model prefix — no hardcoded format constants in server code
- `generate-models.py` produces enriched `models.json` (metadata + recommendations + coordFormats) → TS consumes, zero hardcoded model names
- Optional Artificial Analysis API enrichment (ARTIFICIAL_ANALYSIS_API_KEY)
- `get_interactive_elements` accepts `method` param ("default"/"vlm") → detection strategy, VLM usable on any target (browser, desktop, arbitrary images)
- VLM tools accept per-call model override → agents can switch models without changing extension config
- VS Code extension `vscode-extension/` (TypeScript, SecretStorage for API keys)
- Dynamic over hardcoded: sizes, coordinates, thresholds inferred at runtime or configurable via extension settings — hardcoded pixel values are red flag
- Browser/desktop parity: adding feature for one system that exists in the other → study existing implementation first, match return types and structure
- Tool docstrings = agent-facing docs — keep precise

## Webview Conventions

- Webview content MUST be built via separate pipeline (TSX/esbuild). Never inline HTML/CSS/JS in TypeScript template literals.
- Rendering: JSON data → DOM projection only. Never string-interpolate user data into HTML/JS attributes.
- Use `document.createElement` + `textContent` + `addEventListener`, never `innerHTML` or inline event handlers with interpolated data.
- Extension sends typed cell/update JSON, webview has generic renderer — schema-projected cells pattern.
- External `<script src>` and `<link href>` require `${webview.cspSource}` in the corresponding CSP directive alongside any nonce. Omitting it silently blocks the resource.

## Coordinate System

X11 with CSD (client-side decorations) has multiple coordinate spaces:

- AT-SPI `CoordType.WINDOW` → relative to client area (below title bar)
- `maim -i` → captures visible window including title bar, excludes shadows
- `xdotool --window` → relative to X11 window top-left (includes invisible shadow frame)
- `_GTK_FRAME_EXTENTS` → shadow margins, `_MUTTER_FRAME_EXTENTS` → title bar height
- `CoordTransform` in desktop.py handles all space conversions (VLM resize, crop offset, frame offsets)
- `CoordTransform` carries 4 shadow fields (shadow_left/right/top/bottom) from `_GTK_FRAME_EXTENTS`
- Shadow frame cropping removes CSD shadows from maim captures, fixing ~35px VLM offset on GNOME

## Core Principle

MCP tools are the product. Tool can't do something → fix tool code. Never work around with shell commands, raw utilities, or manual steps. Every interaction is a test of the product.
MCP tools are always available in the agent tool palette. Call them directly — never search for them, try to "load" them, or work around them with shell commands.

## MCP Tool Parameters

Common params across tools — use exact names, not variants:

- `window` — desktop window title substring (not `window_title`, not `title`). Omit for browser.
- `session` — browser session name, defaults to `"default"`. Mutually exclusive with `window`.
- `method` — element detection: `"default"` (AT-SPI + VLM fallback) or `"vlm"` (force VLM). Desktop only.
- `model` — per-call VLM model override. Omit to use extension-configured model.
- `debug_dir` — always `"out/vscode"` when debugging. Server auto-generates timestamped subdirs.

Wrong param names are silently ignored — the tool falls back to browser session instead of erroring.

## Validation

Validate against real VS Code window using only interact MCP tools.
Debug output → `debug_dir` param must always be `"out/vscode"` — never custom subdirs like `out/vscode/comparison/` or `out/my-test/`.
Server auto-generates structure: `out/vscode/{YYYYMMDD_HHMMSS}/{HHMMSS}_{tool}/`. Session timestamp groups all calls; tool timestamp identifies each invocation.
Real-app debug sessions reveal integration bugs invisible to unit tests → analyze `out/` after testing against non-trivial apps.
Test with real VLM calls against actual windows using interact MCP tools. Unit tests alone do not validate VLM integration.
Tool fails → fix tool code, never use workarounds.
Model validation workflow: `configured_providers` → pick models → `get_interactive_elements` with model override → compare results.
Annotated images → always view after generating. Check box offset (up/down/left/right), box size relative to elements, correct overlap with actual UI.
Unit tests: `uv run pytest tests/ -q --tb=short` — pass before committing.

- Webview changes are not done until the webview is opened and rendered content is visually verified. A screenshot of the panel/button that triggers it is NOT sufficient — the content inside must be visible. Empty/blank = broken.

### Output directory layout

Invariant: only two top-level categories under `out/` — `vscode/` (MCP debug, default `debug_dir`) and `tests/` (test artefacts). Dated session folders live INSIDE the category, never at the root. Forbidden: `out/e2e/`, `out/{YYYYMMDD_HHMMSS}/`, or any other top-level sibling.

```
out/
  vscode/                              ← MCP server debug (debug_dir="out/vscode")
    {YYYYMMDD_HHMMSS}/                   session timestamp
      {HHMMSS}_{tool}/                     tool invocation
  tests/
    e2e/                               ← E2E real-LLM tests (uv run pytest tests/e2e/)
      {YYYYMMDD_HHMMSS}/                   session timestamp (one per pytest run)
        {provider}/{HHMMSS}_{test}/          per-provider, per-test artifacts
          input.png                            raw screenshot sent to VLM
          annotated_vlm.png                    VLM detections drawn on screenshot
          annotated_gt.png                     ground truth drawn on screenshot
          vlm_elements.json                    parsed VLM response
          ground_truth.json                    AT-SPI / DOM elements
          comparison.json                      match analysis
          interpretation.txt                   human-readable summary
        results.json                         all results across providers (this session)
    detect/                            ← CLI detection runner (interact detect)
      {YYYYMMDD_HHMMSS}/                   session timestamp
        {provider}/{HHMMSS}_{model_id}/      per-model artifacts
          input.png                            raw screenshot
          annotated_vlm.png                    VLM detections drawn
          annotated_gt.png                     ground truth drawn
          vlm_elements.json                    parsed VLM response
          vlm_raw.txt                          raw VLM text output
          vlm_meta.json                        model/format/resize metadata
          ground_truth.json                    AT-SPI elements
          summary.txt                          human-readable results
```

Key artifacts for debugging detection issues:

- **annotated_vlm.png** — what the model "saw" (boxes it predicted). Check position accuracy.
- **annotated_gt.png** — ground truth from AT-SPI/DOM. Compare visually with VLM.
- **interpretation.txt** — match rate, offset stats, per-element pass/fail.

Never commit `out/` contents (gitignored). Clean with `rm -rf out/`.

### Test types

- **Unit tests** (`uv run pytest tests/ -q --tb=short`) — fast, mocked, run before every commit. Gate commits. Excludes e2e via `norecursedirs`.
- **E2E tests** (`uv run pytest tests/e2e/ -v -s`) — real LLM calls, default OpenAI. All providers: `--all-providers`.
- **CLI detect** (`uv run interact detect`) — standalone detection runner with configurable model (`-m`), window (`-w`), or `--all-providers`. Outputs to `out/tests/{session}/detect/`.
- **Integration tests** (via interact MCP tools in VS Code chat) — real VLM calls, real windows, manual verification. Gate "done".
- **Test GUI** (`python tests/fixtures/test_gui.py`) — GTK window for real desktop interaction testing. Only window exposing AT-SPI nodes reliably.

### Test conventions

- HTML/CSS/JS fixtures go in `tests/fixtures/` as separate files — never embedded in Python strings.
- Tests use project's own abstractions (`BrowserManager`, `_vlm_detect_elements`) — never raw Playwright or litellm directly.
- Test data models use Pydantic BaseModel — not dataclasses or dicts.
- Comparison/assertion logic belongs on the model (methods), not procedural functions.
- E2E tests produce annotated images (`annotated_vlm.png`, `annotated_gt.png`) for every detection run — always view before declaring pass.

### Definition of Done (VLM changes)

- Run `uv run pytest tests/e2e/ -v -s` after any VLM/detection change. View annotated images in `out/tests/{session}/e2e/`.

### Annotation method validation (mandatory before done)

Always test all annotation pipelines using the actual installed interact MCP tools (not simulated):

1. **AT-SPI**: `get_interactive_elements` on GTK test window (`python tests/fixtures/test_gui.py`) — ONLY window that exposes AT-SPI nodes. Electron apps (VS Code, Chrome) do not expose AT-SPI.
2. **VLM**: `get_interactive_elements` with `method="vlm"` on same GTK test window — enables direct comparison with AT-SPI positions
3. **Browser DOM**: `get_interactive_elements` on browser session (only when browser changes involved)
4. **Cross-compare**: VLM boxes must closely match AT-SPI positions for same elements — compare (x, y, w, h) numerically, use VLM query to verify box alignment visually
5. **View annotated screenshots** from both AT-SPI and VLM — verify boxes align with actual elements
6. Tool output must include resolution metadata (model used, method resolved, image dimensions) — verify it appears

### Testing isolation

- AT-SPI/VLM desktop testing → GTK test window (`python tests/fixtures/test_gui.py`). Never use Electron apps for AT-SPI validation.
- Browser testing → dedicated window (`code /tmp/test-workspace` or launch browser via MCP).
- Never test on user's active window.
- Never modify user's VS Code settings — use env vars (`INTERACT_COMPONENT_MODEL=haiku uv run ...`) or test workspace with own `.vscode/settings.json`.
- VLM/model testing → cloud APIs via extension SecretStorage, never local model installation.
- Never `ollama pull` or run local models — use `ollama/model:cloud` variants routed via Ollama Cloud API (OLLAMA_API_KEY). litellm handles routing, no local OLLAMA_API_BASE needed.
- `type_text` targets focused element in window (chat, terminal, editor), not command palette overlay → use `key_press` for command palette navigation.
- Testing windows must NOT steal user focus. Never move the user's cursor, bring windows to foreground, or interact with their active window.
- User's active window: read-only screenshots ONLY. Never click, type, or interact with it.
- Test VS Code instances: prefer background launch or inform user. Operate only via MCP tools, never via xdotool/xprop/wmctrl directly.

### Credential boundaries

- Never attempt to read, decrypt, or access VS Code SecretStorage, keyring, or credential files directly.
- Use only the `configured_providers` MCP tool to discover available providers.
- API keys are injected as env vars by the extension when spawning the MCP server — the server never reads them from storage.
- Never manually look up API keys, grep environment variables, or read credential files.

### Extension delivery (mandatory after any `vscode-extension/` change)

- Build + install: `cd vscode-extension && npm run compile && vsce package && code --install-extension *.vsix`
- Reload VS Code window after install — stale extension code runs until reload.
- Pre-commit hook builds .vsix but install + reload are manual.
- Never declare done without reload + visual verification.
- Command palette via MCP: `type_text` with `clear_first: false` → preserves `>` prefix.

## Grounding benchmark

Real GUI-grounding scores come from the HuggingFace dataset `rootsautomation/ScreenSpot` (or `TIGER-Lab/ScreenSpot-Pro` for the hard variant). Run `uv run interact-bench-grounding --n 30` to evaluate every model with an API key in the current env via `Model.by_capability(GUI_GROUNDING)`. Results are written to `src/interact/data/grounding_results.json` and hydrated into `Model.benchmarks` at registry load (`INTERACT_GROUNDING_PATH` / `INTERACT_GROUNDING_JSON`). No model names or leaderboard scores are hardcoded anywhere — the dataset is the single source of truth.

## Cost & evaluation discipline

- Never run paid grounding / benchmark / scoring evals to obtain numbers already present in `src/interact/data/published_scores.json`. Query `PublishedTable.by_id(...)` / `best_published(...)` first.
- Published source has the (model, dataset) pair → use the published number, no API call.
- Published source lacks it → still no paid eval without explicit budget approval from the user in the same message.
- One run per (model, dataset, n) is the cap. Repeated runs require explicit user re-run request.
- Applies to any cost: tokens, API credits, compute hours, third-party charges.
- `interact-bench-grounding` is a DEVELOPER-ONLY terminal utility. Do not surface it as a VS Code command (`package.json` `contributes.commands`), dashboard / webview button, menu item, or any other end-user entry point. Missing published number → add a published source / TODO, never a UI button to compute one. Classify model status (obsolete / supported / premium) from benchmark data columns (e.g. `intelligence_score`, presence in latest publication), never from hand-curated name regexes.

## Git

Relaxed mode — direct main commits, no branches.
Pre-commit hook auto-builds `.vsix` on `vscode-extension/` changes.

### Conventional Commits

Format: `type(scope): description` — lowercase, imperative, max 72 chars.
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`.
Scope: optional module identifier (`vision`, `desktop`, `extension`, `config`).
Breaking changes: `feat!:` or `fix!:` prefix, or `BREAKING CHANGE:` in body.
Git agent owns commit message formatting.
