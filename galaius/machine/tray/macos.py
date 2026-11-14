"""macOS: the icon in the menu bar (an `NSStatusItem` of an accessory application: no Dock icon), its
menu opened by a click."""

from io import BytesIO
from typing import ClassVar

import AppKit
import Foundation
import objc

from galaius.machine.tray.view import TrayBackend, TrayDriver, TrayView


class GalaiusTrayTarget(Foundation.NSObject):
    """What the timer and the menu rows call (Objective-C needs an object with selectors)."""

    def initWithTray_(self, tray: "MacTray") -> "GalaiusTrayTarget":
        self = objc.super(GalaiusTrayTarget, self).init()
        if self is None:
            return None
        self.tray = tray
        return self

    def tick_(self, timer: object) -> None:
        self.tray.ticked()

    def act_(self, sender: AppKit.NSMenuItem) -> None:
        self.tray.clicked(int(sender.tag()))


class MacTray(TrayBackend):
    #: The menu bar's icon height in points; drawn at twice that for Retina screens.
    points: ClassVar[int] = 18

    def __init__(self) -> None:
        self.driver: TrayDriver | None = None
        self.item = None
        self.target = None
        self.menu_view: TrayView | None = None

    def run(self, driver: TrayDriver) -> None:
        self.driver = driver
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.target = GalaiusTrayTarget.alloc().initWithTray_(self)
        self.show(driver.tick())
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(TrayView.frame_seconds, self.target, "tick:", None, True)
        app.run()

    def ticked(self) -> None:
        if self.driver.leaving:  # out of `run`: the run loop stops once an event arrives, this one
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
            AppKit.NSApp.stop_(None)
            AppKit.NSApp.postEvent_atStart_(AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                AppKit.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0), True)
        else:
            self.show(self.driver.tick())

    def show(self, view: TrayView | None) -> None:
        if view is None:
            return
        buffer = BytesIO()
        view.picture(self.points * 2).save(buffer, format="PNG")
        image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue())))
        image.setSize_((self.points, self.points))
        button = self.item.button()
        button.setImage_(image)
        button.setToolTip_(view.tooltip)
        if self.menu_view is None or self.menu_view.items != view.items:
            menu = AppKit.NSMenu.alloc().init()
            menu.setAutoenablesItems_(False)
            for index, row in enumerate(view.items, start=1):
                entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(row.label, "act:" if row.action is not None else None, "")
                entry.setTarget_(self.target)
                entry.setTag_(index)
                entry.setEnabled_(row.action is not None)
                menu.addItem_(entry)
            self.item.setMenu_(menu)
            self.menu_view = view

    def clicked(self, index: int) -> None:
        if self.menu_view is not None and 1 <= index <= len(self.menu_view.items) and (action := self.menu_view.items[index - 1].action) is not None:
            self.driver.act(action)

    def open(self, url: str) -> None:
        AppKit.NSWorkspace.sharedWorkspace().openURL_(Foundation.NSURL.URLWithString_(url))
