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

## Configuration

All settings are environment variables with the `INTERACT_` prefix.

| Variable                           | Default                   | Description                                                                          |
| ---------------------------------- | ------------------------- | ------------------------------------------------------------------------------------ |
| `INTERACT_IMAGE_MODEL`         | `gpt-4o`                  | litellm model string for image (screenshot) analysis                                 |
| `INTERACT_VIDEO_MODEL`         | `gemini/gemini-2.0-flash` | litellm model string for video analysis                                              |
| `INTERACT_IMAGE_BASE_URL`      | _(none)_                  | Custom endpoint for image model (e.g. local Ollama, Azure)                           |
| `INTERACT_VIDEO_BASE_URL`      | _(none)_                  | Custom endpoint for video model                                                      |
| `INTERACT_HEADLESS`            | `true`                    | Run browser headlessly                                                               |
| `INTERACT_BROWSER_TYPE`        | `chromium`                | `chromium`, `firefox`, or `webkit`                                                   |
| `INTERACT_VIEWPORT_WIDTH`      | `1280`                    | Browser viewport width                                                               |
| `INTERACT_VIEWPORT_HEIGHT`     | `720`                     | Browser viewport height                                                              |
| `INTERACT_SCREENSHOT_DUMP_DIR` | _(none)_                  | When set, saves every screenshot as a PNG file to this folder — useful for debugging |

### API key resolution

API keys are resolved automatically from standard provider environment variables based on the model prefix. No interact-specific key variables are needed.

| Provider  | Environment variable | Models                                                   |
| --------- | -------------------- | -------------------------------------------------------- |
| OpenAI    | `OPENAI_API_KEY`     | `gpt-*`, `o1-*`, `o3-*`, `o4-*`, `chatgpt-*`, `openai/*` |
| Google    | `GEMINI_API_KEY`     | `gemini/*`                                               |
| Anthropic | `ANTHROPIC_API_KEY`  | `claude-*`, `anthropic/*`                                |
| ZAI       | `ZAI_API_KEY`        | `zai/*` (falls back to `Z_AI_API_KEY`)                   |

### Vision model examples

```bash
# OpenAI (default image model)
OPENAI_API_KEY=sk-...

# Google Gemini (default video model)
GEMINI_API_KEY=...

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
INTERACT_IMAGE_MODEL=claude-3-5-sonnet-20241022

# ZAI
ZAI_API_KEY=...
INTERACT_IMAGE_MODEL=zai/glm-4.5v

# Local (no key needed)
INTERACT_IMAGE_MODEL=ollama/llava
INTERACT_IMAGE_BASE_URL=http://localhost:11434
```

---

## MCP client setup

### Claude Desktop

```json
{
  "mcpServers": {
    "interact": {
      "command": "uvx",
      "args": ["interact", "mcp"],
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "GEMINI_API_KEY": "..."
      }
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

## No system prompt

Vision calls do not include a system prompt. The `query` parameter you pass to any tool becomes the user-facing prompt the model receives alongside the page data. You control all framing.
