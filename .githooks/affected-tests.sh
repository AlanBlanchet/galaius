#!/usr/bin/env bash
# The tests a push to public main touches, run here on this computer before GitHub runs them on three systems:
# each test file the commits change, and each one naming a module they change (`galaius.a.b`, or `from galaius.a
# import b`). A red push to main holds every galaius slice until a fix lands (two reached CI red on 2026-11-17, one
# failing on Linux and macOS alike), and costs the owner a failure mail. Free and local; a Windows-only behaviour
# still needs a Windows run.
#   affected-tests.sh RANGE...   (git log range arguments; exit 1 when one of those tests fails)
# Run as GitHub runs them: on a systemd computer each batch is a transient user service (`systemd-run --user`), so no
# agent run's scope, variables or login reach a test's own child processes (an agent's scope above a test's
# subprocess made galaius read that run's identity: a false red on a green tree), with its own memory ceiling
# (PREPUSH_TEST_MEMORY, 4G) inside the agents' slice; elsewhere the pushing agent's variables are stripped. The locale is
# GitHub's too (C.UTF-8, English): a pusher's own language (LANG=fr_FR.UTF-8) made tests asserting English sentences fail.
set -uo pipefail
BATCH="${PREPUSH_TEST_BATCH:-8}" MEMORY="${PREPUSH_TEST_MEMORY:-4G}"
# shellcheck disable=SC2068
changed="$(git log --name-only --diff-filter=d --format= $@ -- '*.py' | sort -u)"
[ -n "$changed" ] || exit 0
picked="$(printf '%s\n' "$changed" | grep -E '^tests/.*test_[^/]*\.py$' || true)"
for path in $(printf '%s\n' "$changed" | grep -E '^galaius/.*\.py$'); do
  module="${path%.py}"; module="${module%/__init__}"; module="${module//\//.}"
  parent="${module%.*}" leaf="${module##*.}"
  picked+=$'\n'"$(grep -rlE --include='test_*.py' "\\b${module//./\\.}\\b|from ${parent//./\\.} import [^#]*\\b${leaf}\\b" tests 2>/dev/null)"
done
mapfile -t files < <(printf '%s\n' "$picked" | grep -v '^$' | sort -u | while read -r file; do [ -f "$file" ] && echo "$file"; done)
[ "${#files[@]}" -gt 0 ] || exit 0
echo "pre-push: ${#files[@]} test file(s) touched by this push, run here first (Linux, batches of ${BATCH}):" >&2

# The batches start when this computer is quiet: its tests have per-test time bounds (90 s, a cold console child, an ffmpeg clip) a
# computer loaded by the agents running on it breaks (2026-11-19: load average 13 to 107 on 12 cores, a different test red on
# every push, each passing alone). Waits while the 1-minute load average is over PREPUSH_QUIET_LOAD (default: the core count)
# for at most PREPUSH_QUIET_SECONDS (default 1200) once per push, then runs anyway; no /proc/loadavg (not Linux): no wait.
quiet() {
  [ -r /proc/loadavg ] || return 0
  local limit="${PREPUSH_QUIET_LOAD:-$(nproc 2>/dev/null || echo 4)}" until=$((SECONDS + ${PREPUSH_QUIET_SECONDS:-1200})) load told=0
  case "$limit" in ''|*[!0-9.]*) echo "pre-push: PREPUSH_QUIET_LOAD=$limit is no number: not waiting" >&2; return 0;; esac
  while read -r load _ < /proc/loadavg && awk -v current="$load" -v wanted="$limit" 'BEGIN { exit !(current > wanted) }' && [ "$SECONDS" -lt "$until" ]; do
    [ "$told" = 0 ] && echo "pre-push: waiting for this computer to be quiet (load average $load, wanted under $limit; at most ${PREPUSH_QUIET_SECONDS:-1200} s)" >&2
    told=1; sleep $(( until - SECONDS < 15 ? until - SECONDS : 15 ))
  done
}

pytest_batch() {  # files...: one pytest process, as GitHub runs it
  # A per-test bound of 300 s here (GitHub keeps pyproject's 90 s): its thread timeout ends the whole batch process, and a computer
  # loaded by the agents on it makes a healthy 11 s test take over 90 s (2026-11-20: 4 pushes lost to it).
  local test=(uv run --frozen -q pytest -m "not integration" -q -rfE -p no:cacheprovider -o timeout="${PREPUSH_TEST_TIMEOUT:-300}" "$@")
  if command -v systemd-run >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    systemd-run --user --quiet --wait --pipe --collect --slice=galaius-agents.slice -p MemoryMax="$MEMORY" \
      -p WorkingDirectory="$PWD" -E PATH="$PATH" -E HOME="$HOME" -E LANG=C.UTF-8 -E LC_ALL=C.UTF-8 -E LANGUAGE=en "${test[@]}" </dev/null
  else
    # shellcheck disable=SC2046
    env $(env | grep -oE '^(GALAIUS|CLAUDE|CODEX|ANTHROPIC)[A-Z0-9_]*' | sed 's/^/-u /') LANG=C.UTF-8 LC_ALL=C.UTF-8 LANGUAGE=en "${test[@]}" </dev/null
  fi
}

# A test building a wheel offline (test_conversation_console) runs the project's own locked `uv` (on the test PATH),
# whose cache GitHub's first build fills with the build backend; here a pruned cache may lack it: one online build
# with that same uv first.
uv run --frozen -q uv build --wheel --quiet --out-dir out/tests/prepush-wheel >/dev/null 2>&1 \
  || echo "pre-push: could not build the wheel (offline?); a wheel test may fail" >&2
rm -rf out/tests/prepush-wheel
failed=0
quiet  # once for the whole push: a wait per batch would add up to nine times its bound
for ((start = 0; start < ${#files[@]}; start += BATCH)); do
  pytest_batch "${files[@]:start:BATCH}" >&2 || failed=1
done
[ "$failed" = 0 ] && exit 0
echo "pre-push: a test this push touches fails here; it would fail GitHub's checks too. Fix it, then push." >&2
exit 1
