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

## Status measured 2026-08-11 (interact @ 8b4f00f)

| gesture | result |
|---|---|
| wheel over the main list | consumed ✅ |
| wheel over the sheet (#39) | consumed ✅ |
| drag to expand the sheet | ignored ❌ — **but not interact's fault, see below** |

### The drag is Flutter's default, not a defect

Flutter's `ScrollBehavior.dragDevices` **excludes `PointerDeviceKind.mouse` on desktop by
design**, so a mouse *drag* on a scrollable is ignored while the wheel works. A real user with a
real mouse sees exactly the same thing.

Proven by isolation: adding

```dart
scrollBehavior: const _MouseDrag(),   // dragDevices including PointerDeviceKind.mouse
```

to this same fixture — with interact's drag code completely unchanged — made the drag work
immediately. So the synthetic pointer stream is fine; the framework was filtering it by device
kind.

**Guidance:** to scroll a Flutter surface from interact, use `scroll` (wheel), not `drag`. Reach
for `drag` only when the app has opted mouse into `dragDevices`, or for genuinely non-scroll
gestures (reordering, sliders, canvas).

Rejected along the way: a settle after `mouse_down` / before `mouse_up` in `DesktopBackend.drag`
(the usual gesture-arena fix) changed nothing — reverted, not shipped.
