#!/usr/bin/env sh
# Install the `interact` CLI.
#   curl -LsSf https://raw.githubusercontent.com/AlanBlanchet/interact/main/install.sh | sh
#
# Installs uv (the Python tool manager) if missing, then installs `interact`
# globally as a uv tool. Override the source with INTERACT_REPO=<path-or-git-url>.
set -e

REPO="${INTERACT_REPO:-git+https://github.com/AlanBlanchet/interact}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python tool manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin; make it available for the rest of this script
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing interact from ${REPO}…"
uv tool install --force "${REPO}"

# Put ~/.local/bin (uv's tool bin) on PATH for future shells, so `interact` is found.
uv tool update-shell >/dev/null 2>&1 || true

cat <<'DONE'

✓ interact installed.  If `interact` isn't found, open a new shell (PATH was just updated).

  interact            # configuration TUI — models, API keys, usage, bindings (no commands to type)
  interact install <claude|cursor|codex|vscode|windsurf|zed|claude-desktop>   # register the MCP server
  interact status     # what it's bound to + models + keys + usage
  interact doctor     # check keys / providers / Playwright / desktop

VS Code: also install the "Interact" extension (marketplace publisher AlanBlanchet)
for the dashboard + settings UI — it launches the same `interact mcp` server.
DONE
