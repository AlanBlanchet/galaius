"""Waiting for the page to STOP MOVING, so a capture photographs a settled frame.

Two separate mechanisms move a page after the call that started them has already returned, and
each one has produced a filed bug where the pixels disagreed with the DOM:

- a finite CSS transition/animation (#49 — `hover` latched, but the shot caught t≈0);
- a smooth scroll (#109 — `window.scrollY` read the new value while the photo showed the old
  screen). Smooth scrolling is NOT a Web Animations API animation, so `document.getAnimations()`
  cannot see it; it needs its own wait.

Every wait here is bounded and best-effort: a spinner, a marquee, or a page that scrolls itself
forever must never block a tool call.
"""

import asyncio

from playwright.async_api import Page

# ~1s each at 60fps. A page still moving after that is moving on purpose (carousel, marquee).
_SCROLL_FRAMES = 60
_ANIMATION_TIMEOUT_MS = 1000
# A page whose rAF never fires would otherwise wait forever on a frame counter.
_WALL_CLOCK_CAP = 2.0

# One round trip, one rAF loop, both conditions. Splitting into a scroll wait + an animation
# wait cost two evaluates and ~45ms on a page where nothing moved at all.
#
# Scrolling is watched with a CAPTURE-PHASE listener on the document rather than sampling
# window.scrollX/Y. Sampling the window is blind by construction to a scroller that isn't the
# window — a panel, a modal, a virtualised list, anything reached by scrollIntoView — there the
# document never moves, so a position check passes instantly and the capture photographs the
# pre-scroll frame. Scroll events don't bubble, but they do CAPTURE, so one listener on the
# document sees every element's.
#
# Settling needs three frames with no scroll event, not one: a smooth scroll is started by the
# compositor on a frame of its own choosing, and under CPU load can still be at its origin two
# frames after the call that requested it — measured by watching this check pass an annotated
# capture of the pre-scroll screen on a loaded machine.
_SETTLE_JS = """
() => new Promise((resolve) => {
  const CAP = %d, QUIET = 3;
  let frames = 0, lastScroll = 0;
  const onScroll = () => { lastScroll = frames; };
  document.addEventListener('scroll', onScroll, true);
  const done = (n) => { document.removeEventListener('scroll', onScroll, true); resolve(n); };
  const animating = () => {
    try {
      return document.getAnimations()
        .filter(a => { try { return a.effect.getComputedTiming().iterations !== Infinity; }
                       catch (e) { return true; } })
        .some(a => a.playState === 'running');
    } catch (e) { return false; }  // no animations API — the scroll watch is all we have
  };
  const tick = () => {
    frames++;
    if ((frames - lastScroll >= QUIET && !animating()) || frames >= CAP) done(frames);
    else requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})
""" % _SCROLL_FRAMES


async def settle_animations(page: Page, timeout: float = _ANIMATION_TIMEOUT_MS) -> None:
    """Wait (bounded) for FINITE CSS transitions/animations to finish, so a capture taken right
    after a hover shows the FINAL hovered state, not a mid-transition frame — the real cause
    behind "hover doesn't latch": the :hover state DOES apply, but an immediate screenshot caught
    a `duration-500` transition at t≈0 (transform≈none). Infinite animations (spinners) are
    ignored so they can't block; the whole wait is best-effort (#49)."""
    try:
        await page.wait_for_function(
            "() => document.getAnimations()"
            "  .filter(a => { try { return a.effect.getComputedTiming().iterations !== Infinity; }"
            "                 catch (e) { return true; } })"
            "  .every(a => a.playState !== 'running')",
            timeout=timeout,
        )
    except Exception:
        pass  # a looping/again-restarting animation, or no animations API — never block the action


async def settle_page(page: Page) -> None:
    """Wait (bounded) for the page to stop moving — scroll AND finite animations — before a
    capture opens the shutter (#109, #49)."""
    try:
        # Bounded in TIME as well as frames. The frame cap assumes rAF keeps firing, which it
        # doesn't in an occluded or backgrounded window — hanging the whole tool call on a wait
        # whose entire purpose is cheap insurance.
        await asyncio.wait_for(page.evaluate(_SETTLE_JS), _WALL_CLOCK_CAP)
    except Exception:
        pass  # navigated mid-wait, a frozen rAF, or a context that cannot run it — never block
