#!/usr/bin/env sh
# Install the `interact` CLI, then connect this computer to your Interact account.
#   curl -LsSf https://raw.githubusercontent.com/AlanBlanchet/interact/main/install.sh | sh
#
# Needs only curl (or wget): installs uv (the Python tool manager) and a Python if missing, then
# `interact` from GitHub's source archives (no git needed). Run from a terminal, it goes straight on
# to `interact login`. Override the source with INTERACT_REPO=<path-or-git-url>, or another source
# archive of this repository (a branch or tag .tar.gz) with INTERACT_ARCHIVE=<url>.
set -eu

# uv's own installer, this exact release, its bytes pinned (it pins each uv binary's sha256 in turn);
# the same pins as the Interact server's installer.
UV_VERSION="0.11.25"
UV_INSTALLER_SHA256="ca2de1bca2913ba30ce88658b6d90a663c627ecac378803aa58084a9adb35a46"

main() {
  fetch_tool
  if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv ${UV_VERSION} (Python tool manager)…"
    uv_installer="$(mktemp)"
    fetch "https://astral.sh/uv/${UV_VERSION}/install.sh" > "$uv_installer"
    [ "$(sha256 "$uv_installer")" = "$UV_INSTALLER_SHA256" ] || { rm -f "$uv_installer"; echo "interact: the uv installer is not the expected file (checksum differs); nothing was installed" >&2; exit 1; }
    sh "$uv_installer"
    rm -f "$uv_installer"
    # uv installs to ~/.local/bin; make it available for the rest of this script
    PATH="$HOME/.local/bin:$PATH"
    export PATH
  fi

  if [ -n "${INTERACT_REPO:-}" ]; then
    echo "Installing interact from ${INTERACT_REPO}…"
    uv tool install --force "${INTERACT_REPO}"
  else
    install_from_archives
  fi

  # Put uv's tool bin on PATH for future shells, so `interact` is found there.
  uv tool update-shell >/dev/null 2>&1 || true
  bin="$(uv tool dir --bin)"

  echo ""
  echo "✓ interact installed."
  # stdin is this script: ask the terminal, and only when there is one.
  if [ -t 1 ] && (exec </dev/tty) 2>/dev/null; then
    echo "Connecting this computer to your Interact account…"
    "$bin/interact" login </dev/tty || echo "Not connected. Run it again any time:  interact login"
  else
    echo "Next, connect this computer to your Interact account:  interact login"
  fi
  echo "(In a new terminal \`interact\` is on your PATH; here: $bin/interact)"
  echo ""
  echo "Also: interact install <claude|cursor|codex|vscode|windsurf|zed|claude-desktop>   # register the MCP server"
  echo "      interact status | interact doctor | interact    # bindings, checks, settings UI"
}

# The main branch and the exact interact-core it pins, as source archives: no git on the computer.
install_from_archives() {
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT INT TERM
  echo "Downloading interact…"
  # main's exact commit: the archive's folder then names it, and the install knows which build it is
  # (an automatic upgrade never re-installs it). No answer from GitHub's API: main as is.
  commit="$(fetch https://api.github.com/repos/AlanBlanchet/interact/commits/main 2>/dev/null | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1 || true)"
  default="https://github.com/AlanBlanchet/interact/archive/${commit:-refs/heads/main}.tar.gz"
  fetch "${INTERACT_ARCHIVE:-$default}" | tar -xz -C "$work"
  source_dir="$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  core="$(sed -n 's/.*interact-core\.git@\([0-9a-f]\{40\}\).*/\1/p' "$source_dir/pyproject.toml")"
  [ -n "$core" ] || { echo "interact: cannot read the pinned interact-core version" >&2; exit 1; }
  printf 'interact-core @ https://github.com/AlanBlanchet/interact-core/archive/%s.tar.gz\n' "$core" > "$work/overrides.txt"
  echo "Installing interact (this takes a minute the first time)…"
  uv tool install --force --quiet --overrides "$work/overrides.txt" "$source_dir"
}

fetch_tool() {
  if command -v curl >/dev/null 2>&1; then FETCH=curl
  elif command -v wget >/dev/null 2>&1; then FETCH=wget
  else echo "interact: install curl or wget first" >&2; exit 1
  fi
}

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() {
  if [ "$FETCH" = curl ]; then curl -LsSf "$1"; else wget -qO- "$1"; fi
}

main "$@"
