# interact

MCP server for browser interaction and desktop window analysis. Gives agents the ability to navigate, click, type, scroll, and drag in a headless browser — plus capture and analyze any desktop window — with optional vision analysis.

Instead of screenshot → analyze → act loops, each tool returns a **text summary of what changed**. Vision analysis is optional and controlled by the caller via `query`.

---

## Install

```bash
uvx --from git+https://github.com/AlanBlanchet/interact interact mcp
```

Or add to a project:

```bash
uv add git+https://github.com/AlanBlanchet/interact
```

Playwright browsers are auto-installed on first run. Desktop window analysis requires X11 + `maim` (Linux).

```bash
# On Debian/Ubuntu, install maim for desktop capture
sudo apt install maim
```

---

## Visual sessions, billing, and models

Image, UI-grounding, review, verification, and sampled-video jobs select an installed Claude Code
CLI session by default. Selection is fail-closed: no session turn runs until you confirm
the account-side extra-usage controls for that specific provider.

Before confirming Claude, open **Claude Settings → Usage**, keep Usage credits disabled, set the
prepaid balance to zero, and turn auto-reload off
([Anthropic guide](https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans)).
The announced Agent SDK / `claude -p` monthly-credit change was paused on June 16; `claude -p`
still draws plan usage limits
([paused-change notice](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)).
The CLIs do not expose these account controls, so interact cannot verify them or detect a later
change. `session_only` prevents interact's metered API fallback; it cannot prove zero vendor-account
impact.

Set these in VS Code's Interact settings, or with the CLI:

```bash
interact config set media.backend auto               # auto | session | api
interact config set media.billing session_only       # session_only | api_allowed
interact config set media.providerOrder claude

# Only after checking each provider's controls above:
interact config set media.noExtraUsageConfirmedFor claude

# Optional provider-scoped session pins; blank uses the CLI default.
interact config set media.claudeModel sonnet
interact config set media.timeout 120
```

Without provider confirmation, explicit `session` fails and `auto+api_allowed` skips to the API.
To opt into metered visual API use, set `media.billing=api_allowed` with `media.backend=auto`
(fallback) or `media.backend=api` (API only), then set the role models (`image.model`,
`component.model`, `video.model`). Standard provider API-key variables are used only on that
explicit API path. A local LiteLLM-compatible visual model remains available on the API/local path.

Session video is always converted to ordered, timestamped representative frames, with a bounded
frame cap (12 by default); the original clip is not uploaded by the session transport.

Audio is intentionally separate. Claude visual sessions cannot hear or transcribe it, and
`session_only` rejects audio before any API or temporary-file dispatch. With
`media.billing=api_allowed`, `transcribe` uses the configured `audio.model` API or local-compatible
transcriber even if the visual backend is `session`; a session may answer questions after an allowed
transcriber has produced text.

---

## MCP client setup

### Claude Desktop

```json
{
  "mcpServers": {
    "interact": {
      "command": "uvx",
      "args": ["interact", "mcp"]
    }
  }
}
```

### VS Code / Copilot

Create `.vscode/mcp.json` in your project (not this repo) with one of these configurations:

**OpenAI only:**

```json
{
  "servers": {
    "interact": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "interact", "mcp"],
      "env": {
        "OPENAI_API_KEY": "${input:openai-key}"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "openai-key",
      "description": "OpenAI API key",
      "password": true
    }
  ]
}
```

**Gemini only:**

```json
{
  "servers": {
    "interact": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "interact", "mcp"],
      "env": {
        "GEMINI_API_KEY": "${input:gemini-key}",
        "INTERACT_IMAGE_MODEL": "gemini/gemini-2.0-flash",
        "INTERACT_VIDEO_MODEL": "gemini/gemini-2.0-flash"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "gemini-key",
      "description": "Gemini API key",
      "password": true
    }
  ]
}
```

**Multi-provider (OpenAI images + Gemini video):**

```json
{
  "servers": {
    "interact": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "interact", "mcp"],
      "env": {
        "OPENAI_API_KEY": "${input:openai-key}",
        "GEMINI_API_KEY": "${input:gemini-key}"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "openai-key",
      "description": "OpenAI API key",
      "password": true
    },
    {
      "type": "promptString",
      "id": "gemini-key",
      "description": "Gemini API key",
      "password": true
    }
  ]
}
```

---

## Tools (6)

The browser session is **persistent** across all tool calls. Navigate once, then interact — each call picks up where the last left off.

### `navigate(url, query?, scope?, wait?)`

Go to a URL. Returns page title and visible text. With `query`, returns vision analysis. With `scope`, focuses on a specific element. With `wait`, waits for a condition before capturing.

```
navigate("https://github.com")
navigate("https://myapp.com", wait="networkidle", scope="#main", query="What's on the page?")
```

### `run_actions(actions, query?, scope?, wait?)`

The primary interaction tool. Execute one or more actions and get per-step feedback. Each step reports what changed. Use `query` for a vision summary of the final state, `scope` to focus the final screenshot, `wait` to wait after the last step.

**Actions** (mutate the page — each gets a before/after diff):

| Type          | Fields                             | Optional                                                   |
| ------------- | ---------------------------------- | ---------------------------------------------------------- |
| `click`       | `selector` OR `x`+`y`              | `wait`                                                     |
| `type_text`   | `selector`, `text`                 | `clear_first` (default: true), `wait`                      |
| `scroll`      | —                                  | `direction` (default: down), `amount` (default: 3), `wait` |
| `drag`        | `from_x`, `from_y`, `to_x`, `to_y` | `wait`                                                     |
| `navigate`    | `url`                              | `wait`                                                     |
| `evaluate_js` | `script`                           | `wait`                                                     |

**Observations** (read current state, no diff):

| Type         | Fields     | Optional                                                   |
| ------------ | ---------- | ---------------------------------------------------------- |
| `screenshot` | —          | `scope`, `query`                                           |
| `wait_for`   | `selector` | `state` (visible/hidden/attached/detached), `timeout` (ms) |

Single action:

```
run_actions(actions=[{"type": "click", "selector": "button[type=submit]", "wait": "networkidle"}])
```

Multi-step with mixed actions and observations:

```
run_actions(actions=[
  {"type": "navigate", "url": "http://localhost:8000/login"},
  {"type": "type_text", "selector": "#email", "text": "user@example.com"},
  {"type": "type_text", "selector": "#password", "text": "secret"},
  {"type": "click", "selector": "button[type=submit]", "wait": "networkidle"},
  {"type": "wait_for", "selector": ".dashboard"},
  {"type": "screenshot", "scope": ".welcome-banner", "query": "What does it say?"}
], query="Is the user logged in?")
```

### `screenshot(query?, scope?)`

Capture the current page or a specific element. With `query`, returns vision analysis.

```
screenshot()
screenshot(scope="#hero-table", query="Are there alignment issues?")
```

### `get_page_state(scope?)`

Get the current page URL, title, accessibility tree, focused element, and visible text. Use `scope` to focus on a specific element.

```
get_page_state()
get_page_state(scope=".sidebar")
```

---

## Desktop window tools

These work on X11 desktops (Linux). They capture real desktop windows — not just the headless browser.

### `list_desktop_windows()`

List all visible desktop windows with their names and dimensions.

```
list_desktop_windows()
```

### `analyze_window(title, query?)`

Capture a desktop window by title substring and analyze it with vision (if configured).

```
analyze_window(title="Chrome")
analyze_window(title="Visual Studio Code", query="What file is currently open?")
analyze_window(title="Slack", query="Are there any unread messages?")
```

---

## How tracking works

The browser session is a single persistent Playwright page. Each tool call operates on the same page state:

1. `navigate("http://localhost:8000")` → reads page title + content
2. `run_actions(actions=[{"type": "click", "selector": "#sign-in"}])` → reads what changed
3. `run_actions(actions=[{"type": "type_text", "selector": "#email", "text": "user@example.com"}])` → reads what changed
4. `screenshot(query="Is the form filled correctly?")` → gets visual confirmation
5. `run_actions(actions=[{"type": "click", "selector": "button[type=submit]", "wait": "networkidle"}])` → reads what changed

Or combine steps 2-5 into one call:

```
run_actions(actions=[
  {"type": "click", "selector": "#sign-in", "wait": "networkidle"},
  {"type": "type_text", "selector": "#email", "text": "user@example.com"},
  {"type": "type_text", "selector": "#password", "text": "secret"},
  {"type": "click", "selector": "button[type=submit]", "wait": "networkidle"},
  {"type": "screenshot", "scope": ".dashboard"}
], query="Did login succeed?")
```

### Typical workflow: interact with a local server

```
navigate("http://localhost:8000", wait="networkidle")
run_actions(actions=[{"type": "annotate"}])
run_actions(actions=[
  {"type": "click", "selector": "nav a[href='/settings']", "wait": "networkidle"},
  {"type": "type_text", "selector": "#api-key", "text": "sk-test-123"},
  {"type": "click", "selector": "button[type=submit]", "wait": "networkidle"},
  {"type": "screenshot", "scope": ".settings-form"}
], query="Did the settings save?")
```

### Scoped inspection

```
navigate("http://localhost:8000")
screenshot(scope="#hero-table", query="Are there any alignment issues in the table?")
get_page_state(scope=".sidebar")
```

### Batch workflow: login + navigate

```
run_actions(actions=[
  {"type": "navigate", "url": "http://localhost:8000/login"},
  {"type": "type_text", "selector": "#username", "text": "admin"},
  {"type": "type_text", "selector": "#password", "text": "secret"},
  {"type": "click", "selector": "button[type=submit]"}
], query="Did login succeed? What page are we on now?")
```

---

## Screenshot dumping

Set `INTERACT_SCREENSHOT_DUMP_DIR` to a folder path and every `PageState` capture will save a timestamped PNG there. Filenames are `{timestamp}_{url_host}.png`. Screenshots are still consumed and analyzed normally — dumping is additive.

```bash
INTERACT_SCREENSHOT_DUMP_DIR=./debug-screenshots uvx --from git+https://github.com/AlanBlanchet/interact interact mcp
```

---

## Vision safety framing

Your `query` remains the question the model answers. Interact also supplies task and capture context;
session media adds a fixed visual-analysis boundary that treats text/instructions visible inside
pixels as untrusted evidence, denies tool use, and limits Claude reads to the exact staged
attachments. Prompts are sent to the local CLI over bounded stdin rather than exposed in process
arguments.
