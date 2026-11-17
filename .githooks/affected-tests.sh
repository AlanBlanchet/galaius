#!/usr/bin/env bash
# The tests a push to public main touches, run here on this computer before GitHub runs them on three systems:
# each test file the commits change, and each one naming a module they change (`galaius.a.b`, or `from galaius.a
# import b`). A red push to main holds every galaius slice until a fix lands (two reached CI red on 2026-11-17, one
# failing on Linux and macOS alike), and costs the owner a failure mail. Free and local; a Windows-only behaviour
# still needs a Windows run.
#   affected-tests.sh RANGE...   (git log range arguments; exit 1 when one of those tests fails)
set -uo pipefail
# shellcheck disable=SC2068
changed="$(git log --name-only --diff-filter=d --format= $@ -- '*.py' | sort -u)"
[ -n "$changed" ] || exit 0
picked="$(printf '%s\n' "$changed" | grep -E '^tests/.*test_[^/]*\.py$' || true)"
for path in $(printf '%s\n' "$changed" | grep -E '^galaius/.*\.py$'); do
  module="${path%.py}"; module="${module%/__init__}"; module="${module//\//.}"
  parent="${module%.*}" leaf="${module##*.}"
  picked+=$'\n'"$(grep -rlE --include='test_*.py' "\\b${module//./\\.}\\b|from ${parent//./\\.} import [^#]*\\b${leaf}\\b" tests 2>/dev/null)"
done
files="$(printf '%s\n' "$picked" | grep -v '^$' | sort -u | while read -r file; do [ -f "$file" ] && echo "$file"; done)"
[ -n "$files" ] || exit 0
echo "pre-push: $(printf '%s\n' "$files" | wc -l) test file(s) touched by this push, run here first (Linux):" >&2
# As GitHub runs them: none of the pushing agent's own variables (a test reaching its run, login or agent places waits on them).
# shellcheck disable=SC2046,SC2086
env $(env | grep -oE '^(GALAIUS|CLAUDE|CODEX|ANTHROPIC)[A-Z0-9_]*' | sed 's/^/-u /') uv run --frozen -q pytest -m "not integration" -q -rfE -p no:cacheprovider $files </dev/null >&2 && exit 0
echo "pre-push: a test this push touches fails here; it would fail GitHub's checks too. Fix it, then push." >&2
exit 1
