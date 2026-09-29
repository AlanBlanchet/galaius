# Sourced by the tracked hooks. This repository is PUBLIC: the private-material denylist (one
# case-insensitive ERE per line, `#` comments) lives in <git-common-dir>/info/private-terms, a file
# git never tracks, so the list itself never reaches the public repository.

# The denylist as one alternation; empty (with a warning) when this checkout has no list.
private_terms() {
    local file
    file="$(git rev-parse --path-format=absolute --git-common-dir)/info/private-terms"
    if [ ! -f "$file" ]; then
        echo "[hook] WARN: $file is missing; private-term scan skipped" >&2
        return 0
    fi
    grep -Ev '^[[:space:]]*(#|$)' "$file" | paste -sd '|' -
}

# The lines of stdin matching the denylist (nothing when the list is empty).
private_hits() {
    local terms="$1"
    [ -n "$terms" ] || { cat >/dev/null; return 0; }
    grep -iE "$terms" | head -5 | cut -c1-160 || true
}
