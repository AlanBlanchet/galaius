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
