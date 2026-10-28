#!/bin/sh
# A browser in a systemd scope of its own (`galaius.browser.browser_scope`): Playwright starts this file as the browser
# and appends its arguments; every process the browser forks lands in the scope, so its memory ceiling holds for
# the whole browser and an out-of-memory kill stops this browser only, never its neighbours. Chromium gets no session
# bus: through it the browser process moves itself into a scope of its own (`app-org.chromium.Chromium-<pid>.scope`),
# out from under the ceiling.
exec systemd-run --user --scope --quiet --collect --slice="$GALAIUS_SCOPE_SLICE" \
    -p MemoryMax="$GALAIUS_SCOPE_MEMORY_MAX" -p MemorySwapMax=0 -- \
    env DBUS_SESSION_BUS_ADDRESS=unix:path=/dev/null "$GALAIUS_SCOPE_EXECUTABLE" "$@"
