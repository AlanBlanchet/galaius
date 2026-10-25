"""`galaius screen`: whether this computer's screen may be watched from the web, and by which of its
owner's passkeys — decided HERE, never by the server.

`on` opens it for a while, `off` closes it at once and ends a live view, `trust` lets a passkey
waiting since the web offered it watch, `forget` drops one, `status` says all of it.
"""

from typing import Annotated

from cyclopts import App, Parameter

from galaius.desktop.backend import DesktopUnsupportedError
from galaius.desktop.screen import FrameGrabber
from galaius.desktop.screen_badge import STOPPED
from galaius.desktop.screen_marker import ScreenMarker
from galaius.desktop.screen_switch import ScreenSwitch
from galaius.desktop.screen_trust import ScreenIdentity, ScreenTrustFile

screen_app = App(name="screen", help="Let this computer's screen be watched from the web, or stop it.")


def _said(switch: ScreenSwitch) -> str:
    allowance = switch.read()
    if not allowance.live():
        return f"off — {allowance.why}" if allowance.why else "off"
    return "on, until switched off" if allowance.until is None else f"on until {allowance.until:%Y-%m-%d %H:%M} UTC"


@screen_app.command(name="status")
def screen_status() -> None:
    """Whether this computer's screen may be watched, by which passkeys, and what it would show."""
    switch, trust = ScreenSwitch(), ScreenTrustFile().read()
    print(f"screen sharing: {_said(switch)}")
    print(f"setting: {switch.path}")
    print(f"this computer's key: {ScreenIdentity().fingerprint}  (a browser shows the same the first time it connects)")
    print(f"passkeys trusted: {len(trust.trusted) or 'none yet (the first one offered while sharing is on pairs)'}")
    for key in trust.trusted:
        print(f"  {key.credential_id[:12]}  {key.label or 'passkey'}  since {key.added_at:%Y-%m-%d}" if key.added_at else f"  {key.credential_id[:12]}")
    for key in trust.pending:
        print(f"  waiting: {key.credential_id[:12]}  {key.label or 'passkey'}  (galaius screen trust {key.credential_id[:12]})")
    try:
        grabber = FrameGrabber()
    except (DesktopUnsupportedError, OSError) as error:
        print(f"this desktop cannot be captured: {error}")
        return
    try:
        for monitor in grabber.monitors():
            print(f"  screen:{monitor.index}  {monitor.geometry}  {monitor.name}")
    finally:
        grabber.close()


@screen_app.command(name="on")
def screen_on(hours: Annotated[int | None, Parameter(name="--hours")] = None) -> None:
    """Let it be watched from the web for `hours` (8 by default; 0: until `galaius screen off`)."""
    switch = ScreenSwitch()
    switch.turn_on(hours)
    print(f"screen sharing: {_said(switch)}")
    print("A badge on this screen says when someone is watching; its Stop, or Ctrl+Alt+Shift+S, ends the view at once.")
    print(f"This computer's key: {ScreenIdentity().fingerprint}")
    print("The first time a browser connects it shows a key: it must read the same, else close that page.")


@screen_app.command(name="off")
def screen_off() -> None:
    """Close it now: a live view ends within a second and the next one is refused."""
    ScreenSwitch().turn_off()
    print("screen sharing: off")


@screen_app.command(name="trust")
def screen_trust(passkey: str) -> None:
    """Let the waiting passkey whose id starts with PASSKEY watch this computer."""
    try:
        key = ScreenTrustFile().trust(passkey)
    except LookupError as error:
        raise SystemExit(f"{error}: `galaius screen status` lists the waiting ones") from None
    print(f"trusted: {key.credential_id[:12]} {key.label}")


@screen_app.command(name="forget")
def screen_forget(passkey: str) -> None:
    """Stop trusting every passkey whose id starts with PASSKEY (trusted or waiting)."""
    print(f"forgotten: {ScreenTrustFile().forget(passkey)}")


@screen_app.command(name="badge")
def screen_badge(viewer: str = "") -> None:
    """Draw the badge here until it is closed or its Stop is pressed — what a view shows on this
    desktop, to see it. Stop also switches sharing off."""
    marker = ScreenMarker(ScreenSwitch())
    if not marker.show(viewer):
        raise SystemExit("badge: no Python here can draw it (tkinter)")
    code = marker.process.wait() if marker.process else 0
    marker.ended_itself()
    print("badge: Stop pressed — screen sharing is now off" if code == STOPPED else f"badge: closed ({code})")
