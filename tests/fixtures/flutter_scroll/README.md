# Flutter scroll/drag fixture (#38, #39)

A **deliberately static** Flutter app: a 60-row `ListView` behind a `DraggableScrollableSheet`
with its own inner list. No animations anywhere, so two idle captures are byte-identical — which
is what makes a frame diff *evidence*. An animated app defeats this: the frame changes with no
input at all, and a naive diff reports a false PASS (that mistake was made here first).

Not built in CI — the Flutter SDK isn't available there. Rebuild locally:

```bash
export PATH="$PATH:/path/to/flutter/bin"
flutter create --platforms=linux --project-name scrollfix /tmp/scrollfix
cp tests/fixtures/flutter_scroll/main.dart /tmp/scrollfix/lib/main.dart
cd /tmp/scrollfix && flutter build linux --debug
# then: launch_app(".../build/linux/x64/debug/bundle/scrollfix", device="phone")
```

Verify a gesture landed by asserting the capture CHANGED (valid only because the app is static),
and confirm staticness first with two idle captures.

## Status measured 2026-08-06 (interact @ 4448a49)

| gesture | result |
|---|---|
| wheel over the main list | consumed ✅ |
| wheel over the sheet (#39) | consumed ✅ |
| **drag to expand the sheet (#38)** | **ignored ❌** |

Rejected hypothesis: adding a settle after `mouse_down` and before `mouse_up` in
`DesktopBackend.drag` (the usual gesture-arena fix) changed nothing — reverted, not shipped.
Tested drag-first on a pristine sheet too, to rule out the inner list having consumed it.
