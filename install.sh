#!/usr/bin/env sh
# Install the `galaius` CLI, then connect this computer to your Galaius account.
#   curl -LsSf https://raw.githubusercontent.com/AlanBlanchet/galaius/main/install.sh | sh
#
# Needs only curl (or wget): installs uv (the Python tool manager) and a Python if missing, then
# `galaius` from GitHub's source archives (no git needed). Run from a terminal, it goes straight on
# to `galaius login`. Override the source with GALAIUS_REPO=<path-or-git-url>, or another source
# archive of this repository (a branch or tag .tar.gz) with GALAIUS_ARCHIVE=<url>. GALAIUS_ADDRESS=<url>
# names the server; a computer already connected elsewhere moves there when that server holds it
# (the server moved): curl -LsSf …/install.sh | GALAIUS_ADDRESS=https://example.org sh
set -eu

# uv's own installer, this exact release, its bytes pinned (it pins each uv binary's sha256 in turn);
# the same pins as the Galaius server's installer.
UV_VERSION="0.11.25"
UV_INSTALLER_SHA256="ca2de1bca2913ba30ce88658b6d90a663c627ecac378803aa58084a9adb35a46"

main() {
  fetch_tool
  if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv ${UV_VERSION} (Python tool manager)…"
    uv_installer="$(mktemp)"
    fetch "https://astral.sh/uv/${UV_VERSION}/install.sh" > "$uv_installer"
    [ "$(sha256 "$uv_installer")" = "$UV_INSTALLER_SHA256" ] || { rm -f "$uv_installer"; echo "galaius: the uv installer is not the expected file (checksum differs); nothing was installed" >&2; exit 1; }
    sh "$uv_installer"
    rm -f "$uv_installer"
    # uv installs to ~/.local/bin; make it available for the rest of this script
    PATH="$HOME/.local/bin:$PATH"
    export PATH
  fi

  if [ -n "${GALAIUS_REPO:-}" ]; then
    echo "Installing galaius from ${GALAIUS_REPO}…"
    uv tool install --force "${GALAIUS_REPO}"
  else
    install_from_archives
  fi

  # Put uv's tool bin on PATH for future shells, so `galaius` is found there.
  uv tool update-shell >/dev/null 2>&1 || true
  bin="$(uv tool dir --bin)"

  echo ""
  echo "✓ galaius installed."
  if [ -n "${GALAIUS_ADDRESS:-}" ]; then set -- --server "$GALAIUS_ADDRESS"; else set --; fi
  migrated=""
  # Named interact until 2026-10-07: a computer that ran it moves its install once, staying connected.
  if [ -d "$HOME/.interact" ] || [ -d "${XDG_CONFIG_HOME:-$HOME/.config}/interact" ]; then
    if "$bin/galaius" migrate; then migrated=1
    else echo "galaius: part of the former install was not moved (above); fix it, then run  galaius migrate" >&2
    fi
  fi
  # stdin is this script: ask the terminal, and only when there is one.
  if [ -n "$migrated" ]; then
    echo "Your interact install is now galaius; this computer stays connected."
  elif [ -t 1 ] && (exec </dev/tty) 2>/dev/null; then
    echo "Connecting this computer to your Galaius account…"
    "$bin/galaius" login "$@" </dev/tty || echo "Not connected. Run it again any time:  galaius login"
  elif [ $# -gt 0 ]; then
    echo "Connecting this computer to ${GALAIUS_ADDRESS}…"
    "$bin/galaius" login "$@" --yes </dev/null || echo "Not connected. Run it again any time:  galaius login"
  else
    echo "Next, connect this computer to your Galaius account:  galaius login"
  fi
  echo "(In a new terminal \`galaius\` is on your PATH; here: $bin/galaius)"
  echo ""
  echo "Also: galaius install <claude|cursor|codex|vscode|windsurf|zed|claude-desktop>   # register the MCP server"
  echo "      galaius status | galaius doctor | galaius    # bindings, checks, settings UI"
}

# The main branch and the exact galaius-core it pins, as source archives: no git on the computer.
install_from_archives() {
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT INT TERM
  echo "Downloading galaius…"
  # main's exact commit: the archive's folder then names it, and the install knows which build it is
  # (an automatic upgrade never re-installs it). No answer from GitHub's API: main as is.
  commit="$(fetch https://api.github.com/repos/AlanBlanchet/galaius/commits/main 2>/dev/null | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1 || true)"
  default="https://github.com/AlanBlanchet/galaius/archive/${commit:-refs/heads/main}.tar.gz"
  fetch "${GALAIUS_ARCHIVE:-$default}" | tar -xz -C "$work"
  source_dir="$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  core="$(sed -n 's/.*galaius-core\.git@\([0-9a-f]\{40\}\).*/\1/p' "$source_dir/pyproject.toml")"
  [ -n "$core" ] || { echo "galaius: cannot read the pinned galaius-core version" >&2; exit 1; }
  printf 'galaius-core @ https://github.com/AlanBlanchet/galaius-core/archive/%s.tar.gz\n' "$core" > "$work/overrides.txt"
  echo "Installing galaius (this takes a minute the first time)…"
  uv tool install --force --quiet --overrides "$work/overrides.txt" "$source_dir"
}

fetch_tool() {
  if command -v curl >/dev/null 2>&1; then FETCH=curl
  elif command -v wget >/dev/null 2>&1; then FETCH=wget
  else echo "galaius: install curl or wget first" >&2; exit 1
  fi
}

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() {
  if [ "$FETCH" = curl ]; then curl -LsSf "$1"; else wget -qO- "$1"; fi
}

main "$@"
