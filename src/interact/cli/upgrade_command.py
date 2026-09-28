"""`interact upgrade`: what runs, what is installed, and the local controls over automatic upgrades."""

import sys
import time
from pathlib import Path

from cyclopts import App

from interact.config.user import UserConfig
from interact.upgrade.check import UpgradeCheck
from interact.upgrade.store import Runtime, RuntimeStore

upgrade_app = App(name="upgrade", help="Automatic upgrades: status, check now, pin, turn off, go back.")


@upgrade_app.default
def upgrade_status() -> None:
    """Which runtime every long-lived process follows, what is installed, and the last events."""
    check = UpgradeCheck.configured()
    store, config = check.store, check.config
    pointer = store.pointer()
    print(f"Active runtime:   {store.active().label()}  ({store.active().path})")
    if pointer.previous is not None:
        print(f"Previous runtime: {Runtime(path=pointer.previous).label()}")
    if pointer.floor is not None:
        print(f"Newest ever run:  {pointer.floor.label()} (older releases are refused)")
    state = "on" if config.auto_upgrade else "off"
    pin = config.upgrade_pin.strip()
    held = "" if not pin else f", holding build {pin}" if UpgradeCheck.pin_pattern.fullmatch(pin) else f", pin {pin!r} ignored (not a commit)"
    print(f"Automatic:        {state}" + held + f", every {config.upgrade_check_seconds} s from {UpgradeCheck.server() or ('GitHub' if config.upgrade_github else 'no server')}")
    wait = store.next_check() - time.time()
    print(f"Next check:       {'due now' if wait <= 0 else f'in {wait:.0f} s'}")
    if pointer.failed:
        print(f"Failed builds:    {len(pointer.failed)} (never tried again)")
    for event in store.events(8):
        print(f"  {event.at:%Y-%m-%d %H:%M:%S} {event.kind:<11} {event.text}")


@upgrade_app.command(name="check")
def upgrade_check(background: bool = False) -> None:
    """Check for a newer signed release now; install and activate it when there is one."""
    outcome = UpgradeCheck.configured().run()
    if not background:
        print(outcome)


@upgrade_app.command(name="use")
def upgrade_use(runtime: Path | None = None) -> None:
    """Make an installed runtime active and hold it (older ones too: your choice, recorded;
    `interact upgrade pin` with no argument lets releases move it again). No argument: list them."""
    store = RuntimeStore.default()
    installed = sorted((path for path in store.root.iterdir() if Runtime(path=path).receipt() is not None), key=lambda path: path.name) if store.root.is_dir() else []
    if runtime is None:
        for path in installed:
            print(f"{'*' if path == store.active().path else ' '} {Runtime(path=path).label():<40} {path}")
        return
    chosen = Runtime(path=runtime.expanduser().resolve())
    if not chosen.usable() or not (chosen.path.is_relative_to(store.root.resolve()) or chosen.path == Runtime.own().path):
        print(f"{chosen.path} is not a runtime of this computer's store (run: interact upgrade use)", file=sys.stderr)
        raise SystemExit(1)
    store.activate(chosen, explicit=True)
    build = chosen.order()
    if build is not None:
        UserConfig.set("INTERACT_UPGRADE_PIN", build.commit)
    print(f"{chosen.label()} is active" + (f" and held (pin {build.commit[:7]})" if build is not None else "") + "; running processes move to it at their next quiet moment")


@upgrade_app.command(name="off")
def upgrade_off() -> None:
    """Stop checking for releases on this computer (what is installed keeps running)."""
    UserConfig.set("INTERACT_AUTO_UPGRADE", "false")
    print("Automatic upgrades are off on this computer.")


@upgrade_app.command(name="on")
def upgrade_on() -> None:
    UserConfig.set("INTERACT_AUTO_UPGRADE", "true")
    RuntimeStore.default().schedule(0)
    print("Automatic upgrades are on; checking within a few seconds.")


@upgrade_app.command(name="pin")
def upgrade_pin(commit: str | None = None) -> None:
    """Hold this computer on one build, named by its commit (7+ hex characters, shown by
    `interact upgrade`): installed when the server offers it, older than what ran here too (your
    choice, recorded); until then the running build stays. No argument: unpin."""
    if commit:
        if not UpgradeCheck.pin_pattern.fullmatch(commit):
            print(f"{commit!r} is not a commit: 7 to 40 lowercase hex characters, as `interact upgrade` shows them", file=sys.stderr)
            raise SystemExit(2)
        UserConfig.set("INTERACT_UPGRADE_PIN", commit)
    else:
        UserConfig.unset("INTERACT_UPGRADE_PIN")
    RuntimeStore.default().schedule(0)
    print(f"Pinned to {commit}." if commit else "Unpinned: the newest signed release is used.")
