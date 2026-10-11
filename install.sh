#!/usr/bin/env sh
# Install the `galaius` CLI, then connect this computer to your Galaius account.
#   curl -LsSf https://raw.githubusercontent.com/AlanBlanchet/galaius/main/install.sh | sh
#
# Needs only curl (or wget): installs uv (the Python tool manager) and a Python if missing, then
# `galaius` from GitHub's source archives (no git needed); it asks nothing. With GALAIUS_ADDRESS it
# goes on to `galaius login --detach` (approval in the browser). Override the source with GALAIUS_REPO=<path-or-git-url>, or another source
# archive of this repository (a branch or tag .tar.gz) with GALAIUS_ARCHIVE=<url>. GALAIUS_ADDRESS=<url>
# names the server; a computer already connected elsewhere moves there when that server holds it
# (the server moved): curl -LsSf …/install.sh | GALAIUS_ADDRESS=https://example.org sh
# Every run writes its own log (<data folder>/galaius/logs/install-<time>.log): each line shown here and every
# program's output, timestamped. A step that fails is named in one sentence; on a computer already connected
# to Galaius the person is asked whether to send that log (with this computer's own key, to its own server).
set -eu

# uv's own installer, this exact release, its bytes pinned (it pins each uv binary's sha256 in turn);
# the same pins as the Galaius server's installer.
UV_VERSION="0.11.25"
UV_INSTALLER_SHA256="ca2de1bca2913ba30ce88658b6d90a663c627ecac378803aa58084a9adb35a46"

main() {
  fetch_tool
  if ! command -v uv >/dev/null 2>&1; then
    step 'getting uv' "l'installation de uv"
    echo "Installing uv ${UV_VERSION} (Python tool manager)…"
    uv_installer="$(mktemp)"
    fetch "https://astral.sh/uv/${UV_VERSION}/install.sh" > "$uv_installer"
    [ "$(sha256 "$uv_installer")" = "$UV_INSTALLER_SHA256" ] || { rm -f "$uv_installer"; fail "the uv installer is not the expected file (checksum differs); nothing was installed"; }
    sh "$uv_installer"
    rm -f "$uv_installer"
    # uv installs to ~/.local/bin; make it available for the rest of this script
    PATH="$HOME/.local/bin:$PATH"
    export PATH
  fi

  if [ -n "${GALAIUS_REPO:-}" ]; then
    step 'installing Galaius' "l'installation de Galaius"
    echo "Installing galaius from ${GALAIUS_REPO}…"
    uv tool install --force "${GALAIUS_REPO}"
  else
    install_from_archives
  fi

  step 'setting up Galaius' 'la configuration de Galaius'
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
  # Nothing is asked: the sign-in goes on in the browser and in the background.
  if [ -n "$migrated" ]; then
    echo "Your interact install is now galaius; this computer stays connected."
  elif [ $# -gt 0 ]; then
    step 'connecting this computer' 'la connexion de cet ordinateur'
    through_file "$bin/galaius" login "$@" --detach || echo "Not connected. Run it again any time:  galaius login --detach"
  else
    echo "Next, connect this computer to your Galaius account:  galaius login --server https://<your server>"
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
  step 'downloading Galaius' 'le téléchargement de Galaius'
  echo "Downloading galaius…"
  # main's exact commit: the archive's folder then names it, and the install knows which build it is
  # (an automatic upgrade never re-installs it). No answer from GitHub's API: main as is.
  commit="$(fetch https://api.github.com/repos/AlanBlanchet/galaius/commits/main 2>/dev/null | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1 || true)"
  default="https://github.com/AlanBlanchet/galaius/archive/${commit:-refs/heads/main}.tar.gz"
  fetch "${GALAIUS_ARCHIVE:-$default}" | tar -xz -C "$work"
  source_dir="$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  core="$(sed -n 's/.*galaius-core\.git@\([0-9a-f]\{40\}\).*/\1/p' "$source_dir/pyproject.toml")"
  [ -n "$core" ] || fail "cannot read the pinned galaius-core version"
  printf 'galaius-core @ https://github.com/AlanBlanchet/galaius-core/archive/%s.tar.gz\n' "$core" > "$work/overrides.txt"
  step 'installing Galaius' "l'installation de Galaius"
  echo "Installing galaius (this takes a minute the first time)…"
  uv tool install --force --overrides "$work/overrides.txt" "$source_dir"
}

fetch_tool() {
  if command -v curl >/dev/null 2>&1; then FETCH=curl
  elif command -v wget >/dev/null 2>&1; then FETCH=wget
  else fail "install curl or wget first"
  fi
}

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() {
  if [ "$FETCH" = curl ]; then curl -LsSf "$1"; else wget -qO- "$1"; fi
}

describe() { printf 'Galaius install (source %s, server %s)' "${GALAIUS_REPO:-${GALAIUS_ARCHIVE:-GitHub main}}" "${GALAIUS_ADDRESS:-not named}"; }

# >>> install report (src/galaius/data/install_report.sh, verbatim: tests/test_install_scripts.py)
# The install-report part every Galaius install.sh shares (owner 2026-10-11: « The install script should have
# error logs in case of errors, and if there is an error that happens we must prompt the user to ask to send »).
# One copy: galaius/data/install_report.sh; a server inserts it into the script it serves, the repository's
# install.sh carries it verbatim (tests/test_install_scripts.py). The script defines `main` (its install, each part
# named with `step`, a refusal with `fail`) and `describe` (the log's first line), then ends with `run "$@"`.
say() { printf '%s\n' "$1"; }
# The reason, said by `run` in its sentence.
fail() { printf '%s\n' "$1" > "$state/reason"; exit 1; }
text() { if [ -n "$french" ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
# What the person reads if this step fails (« The install stopped while <step>: … »), and a marker for the log only.
step() { text "$1" "$2" > "$state/step"; printf '%s-- %s\n' "$LOG_ONLY" "$1"; }
LOG_ONLY="$(printf '\001')"
stamp() { date '+%Y-%m-%d %H:%M:%S'; }
note() { printf '%s %s\n' "$(stamp)" "$1" >> "$log"; }
tell() { say "$1"; note "$1"; }
# A command that may leave a program running (`galaius login` opens the browser, which inherits its output):
# its output goes through a file, then is shown, so no such program holds the log's pipe and stalls the install.
through_file() {
    if "$@" > "$state/through" 2>&1 < /dev/null; then code=0; else code=$?; fi
    cat "$state/through"
    return "$code"
}

# The run: main's output, both streams, shown as it comes and written to the log with its time. A failure is
# said in one sentence, then, on a computer already connected (machine.json holds its key and its server), the
# question; a yes sends the log there, masked of the home folder (the server removes keys, tokens and addresses
# again). A computer never connected holds no key: nothing can be sent, the log's path is said.
run() {
    case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in fr*) french=1 ;; *) french='' ;; esac
    case "$(uname -s)" in Darwin) data="$HOME/Library/Application Support/galaius" ;; *) data="${XDG_DATA_HOME:-$HOME/.local/share}/galaius" ;; esac
    log="$data/logs/install-$(date '+%Y%m%d-%H%M%S')-$$.log"
    (umask 077; mkdir -p "$data/logs" && : >> "$log") 2>/dev/null || log="$(mktemp)"  # private: paths, names
    state="$(mktemp -d)"
    text 'checking this computer' 'la vérification de cet ordinateur' > "$state/step"
    note "$(describe)"
    note "$(uname -srm), sh $(command -v sh), language ${LC_ALL:-${LC_MESSAGES:-${LANG:-unset}}}"
    mkfifo "$state/output"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in "$LOG_ONLY"*) line="${line#"$LOG_ONLY"}" ;; *) printf '%s\n' "$line" ;; esac
        printf '%s %s\n' "$(stamp)" "$line" >> "$log"
    done < "$state/output" &
    shown=$!
    set +e
    ( set -e; main "$@" ) > "$state/output" 2>&1
    status=$?
    set -e
    wait "$shown" || true
    if [ "$status" -eq 0 ]; then note 'done'; rm -rf "$state"; return 0; fi
    reason="$(cat "$state/reason" 2>/dev/null || true)"
    [ -n "$reason" ] || reason="$(text "a command ended with code $status (see the lines above)" "une commande s'est terminée avec le code $status (voir les lignes ci-dessus)")"
    sentence="$(text "The install stopped while $(cat "$state/step"): $reason" "L'installation s'est arrêtée pendant $(cat "$state/step") : $reason")"
    rm -rf "$state"
    note "ERROR $sentence"
    printf '%s\n' "$sentence" >&2
    offer_report "$sentence"
    exit "$status"
}

offer_report() {
    kept="$(text "The whole log is in $log" "Le journal complet est dans $log")"
    machine="${XDG_CONFIG_HOME:-$HOME/.config}/galaius/machine.json"
    # https only (plain http to this computer itself); the token's characters checked before it is a header.
    server="$(sed -n 's/.*"server_url":"\(https:\/\/[^"/[:space:]][^"[:space:]]*\)".*/\1/p; s/.*"server_url":"\(http:\/\/\(127\.0\.0\.1\|localhost\|\[::1\]\)\(:[0-9]*\)\{0,1\}\)\/\{0,1\}".*/\1/p' "$machine" 2>/dev/null | head -n 1)"
    token="$(sed -n 's/.*"token":"\(iwm_[A-Za-z0-9_-]\{32,252\}\)".*/\1/p' "$machine" 2>/dev/null | head -n 1)"
    if [ -z "$server" ] || [ -z "$token" ]; then
        tell "$(text "$kept (this computer is not connected to Galaius yet: nothing was sent)." "$kept (cet ordinateur n'est pas encore connecté à Galaius : rien n'a été envoyé).")"
        return
    fi
    # stdin is this script when piped into sh: the question goes to the terminal itself, none = no.
    answer=''
    if (true < /dev/tty) 2>/dev/null; then  # a subshell: a failed redirection on a special built-in ends the shell
        host="${server#*://}"; host="${host%%/*}"
        printf '%s' "$(text "Send the error report to Galaius ($host)? [Y/n] " "Envoyer le rapport d'erreur à Galaius ($host) ? [O/n] ")" > /dev/tty
        IFS= read -r answer < /dev/tty || answer='-'
    else
        answer='-'
    fi
    note "asked to send the report: $answer"
    case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
        ''|o|oui|y|yes) ;;
        *) tell "$kept"; return ;;
    esac
    command -v curl >/dev/null 2>&1 || { tell "$(text "The report was not sent (curl is missing). $kept." "Le rapport n'a pas été envoyé (curl est absent). $kept.")"; return; }
    id="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen 2>/dev/null || od -An -N16 -tx1 /dev/urandom | tr -d ' \n' | sed 's/\(.\{8\}\)\(.\{4\}\)\(.\{4\}\)\(.\{4\}\)/\1-\2-\3-\4-/')"
    body="$(mktemp)"
    headers="$(mktemp)"
    printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$token" > "$headers"
    {
        printf '{"id":"%s","message":"' "$(printf '%s' "$id" | tr '[:upper:]' '[:lower:]')"
        printf '%s\n' "$1" | first_590 | json_text | sed 's/\\n$//'
        printf '","detail":"'
        log_tail | json_text
        printf '"}'
    } > "$body"
    sent=''
    for attempt in 1 2; do  # the same id: a send that did land is never stored twice
        if answer="$(curl -sS --proto =https,http --max-time 30 -o /dev/null -w '%{http_code}' -H @"$headers" --data-binary @"$body" "${server%/}/v1/machine/install-report" 2>&1)"; then
            case "$answer" in 20[01]) sent=1; break ;; 4*) break ;; esac
        fi
        sleep 3
    done
    rm -f "$body" "$headers"
    note "report sent: ${sent:-no} ($answer)"
    if [ -n "$sent" ]; then tell "$(text "Report sent to $server ($id). $kept." "Rapport envoyé à $server ($id). $kept.")"
    else tell "$(text "The report was not sent ($answer). $kept." "Le rapport n'a pas été envoyé ($answer). $kept.")"
    fi
}

# The log's last 60 000 bytes from a line start (the server takes 64 KiB), the home folder written ~.
log_tail() {
    tail -c 60000 "$log" | { [ "$(wc -c < "$log")" -le 60000 ] || sed 1d; cat; } | sed "s|$(printf '%s' "$HOME" | sed 's/[|\\.*^$[]/\\&/g')|~|g"
}

# Text as the inside of a JSON string: backslash, quote and tab escaped, each line ended by \n, other control
# characters dropped. Built by concatenation: awks differ on backslashes in a gsub replacement.
json_text() {
    LC_ALL=C awk 'BEGIN { ORS = "" }
        { gsub(/[\001-\010\013-\037\177]/, ""); out = ""; s = $0
          while ((i = match(s, /[\\"\t]/)) > 0) { c = substr(s, i, 1); out = out substr(s, 1, i - 1) (c == "\t" ? "\\t" : "\\" c); s = substr(s, i + 1) }
          print out s "\\n" }'
}

# At most 590 bytes of one line, never ending inside a character (the server takes 600 at most).
first_590() {
    LC_ALL=C awk '{ if (length($0) > 590) { $0 = substr($0, 1, 590); sub(/[\300-\377][\200-\277]*$/, "") } print; exit }'
}
# <<< install report

run "$@"
