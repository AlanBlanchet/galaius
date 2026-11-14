"""Linux: the icon as a StatusNotifierItem with its menu over `com.canonical.dbusmenu`, both on the
session bus (jeepney, pure Python). KDE Plasma, Ubuntu's GNOME (its AppIndicator extension), XFCE,
Cinnamon, LXQt and the Wayland bars host these; X11 and Wayland alike, no GTK or Xlib in this process.
A desktop with no host (stock GNOME without the extension) shows nothing; the icon waits, and appears
as soon as a host does (`StatusNotifierWatcher` taking its name)."""

import logging
import os
import subprocess
import time
from typing import ClassVar

from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, new_error, new_method_call, new_method_return, new_signal
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection
from jeepney.low_level import Message

from galaius.machine.tray.view import TrayDriver, TrayBackend, TrayPicture, TrayView

logger = logging.getLogger(__name__)

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"
PROPERTIES = "org.freedesktop.DBus.Properties"
INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"


class StatusNotifierTray(TrayBackend):
    """One item at `/StatusNotifierItem`, its menu at `/MenuBar`, served from one thread that owns the
    connection: it answers the host's calls and, between them, ticks the driver."""

    item_path: ClassVar[str] = "/StatusNotifierItem"
    menu_path: ClassVar[str] = "/MenuBar"
    #: The icon's sizes handed to the host (it picks the closest to its panel's).
    sizes: ClassVar[tuple[int, ...]] = (22, 32, 48, 64)

    def __init__(self) -> None:
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.view: TrayView | None = None
        self.revision = 1
        self.driver: TrayDriver | None = None

    # ---- what the host reads ------------------------------------------------------------------

    def item_properties(self) -> dict[str, tuple[str, object]]:
        view = self.view
        pixmaps = [(size, size, TrayPicture.argb(view.shown, view.frame, size)) for size in self.sizes] if view is not None else []
        tooltip = view.tooltip if view is not None else "Galaius"
        return {"Category": ("s", "ApplicationStatus"), "Id": ("s", "galaius"), "Title": ("s", "Galaius"),
                "Status": ("s", "Active"), "WindowId": ("i", 0), "IconName": ("s", ""), "IconPixmap": ("a(iiay)", pixmaps),
                "OverlayIconName": ("s", ""), "OverlayIconPixmap": ("a(iiay)", []), "AttentionIconName": ("s", ""),
                "AttentionIconPixmap": ("a(iiay)", []), "AttentionMovieName": ("s", ""),
                "ToolTip": ("(sa(iiay)ss)", ("", [], "Galaius", tooltip)), "ItemIsMenu": ("b", True), "Menu": ("o", self.menu_path)}

    @staticmethod
    def menu_properties() -> dict[str, tuple[str, object]]:
        return {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"), "IconThemePath": ("as", [])}

    def entry(self, index: int) -> dict[str, tuple[str, object]]:
        """Row `index` (1-based: 0 is the root) of the menu, as dbusmenu properties."""
        item = self.view.items[index - 1]
        return {"label": ("s", item.label.replace("_", "__")), "enabled": ("b", item.action is not None), "visible": ("b", True)}

    def layout(self) -> tuple[int, tuple]:
        rows = range(1, len(self.view.items) + 1) if self.view is not None else range(0)
        children = [("(ia{sv}av)", (index, self.entry(index), [])) for index in rows]
        return self.revision, (0, {"children-display": ("s", "submenu")}, children)

    # ---- the connection --------------------------------------------------------------------------

    def run(self, driver: TrayDriver) -> None:
        self.driver = driver
        with open_dbus_connection(bus="SESSION") as connection:
            connection.send_and_get_reply(message_bus.RequestName(self.name))
            rule = MatchRule(type="signal", sender="org.freedesktop.DBus", interface="org.freedesktop.DBus", member="NameOwnerChanged")
            rule.add_arg_condition(0, WATCHER)
            connection.send_and_get_reply(message_bus.AddMatch(rule))
            self.show(connection, driver.tick())
            self.register(connection)
            due = time.monotonic()
            while not driver.leaving:
                try:
                    self.answer(connection, connection.receive(timeout=max(due - time.monotonic(), 0)))
                except TimeoutError:
                    pass
                if time.monotonic() >= due:
                    due = max(due + TrayView.frame_seconds, time.monotonic())
                    self.show(connection, driver.tick())

    def register(self, connection: DBusConnection) -> None:
        """Tells the host this item exists; its answer (an error when no host runs yet) arrives as any message."""
        connection.send(new_method_call(DBusAddress("/StatusNotifierWatcher", bus_name=WATCHER, interface=WATCHER), "RegisterStatusNotifierItem", "s", (self.name,)))

    def show(self, connection: DBusConnection, view: TrayView | None) -> None:
        if view is None:
            return
        before, self.view = self.view, view
        item = DBusAddress(self.item_path, interface=ITEM)
        connection.send(new_signal(item, "NewIcon"))
        if before is None or before.tooltip != view.tooltip:
            connection.send(new_signal(item, "NewToolTip"))
        if before is None or before.items != view.items:
            self.revision += 1
            connection.send(new_signal(DBusAddress(self.menu_path, interface=MENU), "LayoutUpdated", "ui", (self.revision, 0)))

    def answer(self, connection: DBusConnection, message: Message) -> None:
        header = message.header
        if header.message_type == MessageType.signal:
            if header.fields.get(HeaderFields.member) == "NameOwnerChanged" and message.body[2]:
                self.register(connection)  # a host started (or restarted): it lists items registered from now on
            return
        if header.message_type != MessageType.method_call:
            return
        path, interface, member = (header.fields.get(field) for field in (HeaderFields.path, HeaderFields.interface, HeaderFields.member))
        reply = self.reply(message, path, interface, member)
        if reply is not None:
            connection.send(reply)

    def reply(self, message: Message, path: str, interface: str | None, member: str) -> Message | None:
        properties = self.item_properties() if path == self.item_path else self.menu_properties() if path == self.menu_path else None
        if properties is None:
            return new_error(message, "org.freedesktop.DBus.Error.UnknownObject")
        if member == "GetAll":
            return new_method_return(message, "a{sv}", (properties,))
        if member == "Get":
            found = properties.get(message.body[1])
            return new_method_return(message, "v", (found,)) if found is not None else new_error(message, "org.freedesktop.DBus.Error.UnknownProperty")
        if member == "Introspect":
            return new_method_return(message, "s", (self.introspection(path),))
        if path == self.item_path:
            return new_method_return(message)  # Activate, SecondaryActivate, ContextMenu, Scroll: the menu does it all
        rows = range(1, len(self.view.items) + 1) if self.view is not None else range(0)
        match member:
            case "GetLayout":
                return new_method_return(message, "u(ia{sv}av)", self.layout())
            case "GetGroupProperties":
                wanted = message.body[0] or list(rows)
                return new_method_return(message, "a(ia{sv})", ([(index, self.entry(index)) for index in wanted if index in rows],))
            case "GetProperty":
                index, name = message.body
                found = self.entry(index).get(name) if index in rows else None
                return new_method_return(message, "v", (found,)) if found is not None else new_error(message, "org.freedesktop.DBus.Error.InvalidArgs")
            case "Event":
                self.clicked(message.body[0], message.body[1])
                return new_method_return(message)
            case "EventGroup":
                for index, event, _, _ in message.body[0]:
                    self.clicked(index, event)
                return new_method_return(message, "ai", ([],))
            case "AboutToShow":
                return new_method_return(message, "b", (False,))
            case "AboutToShowGroup":
                return new_method_return(message, "aiai", ([], []))
        return new_error(message, "org.freedesktop.DBus.Error.UnknownMethod")

    def clicked(self, index: int, event: str) -> None:
        if event != "clicked" or self.view is None or not 1 <= index <= len(self.view.items):
            return
        if (action := self.view.items[index - 1].action) is not None and self.driver is not None:
            self.driver.act(action)

    def introspection(self, path: str) -> str:
        interface = ITEM if path == self.item_path else MENU
        return (f'<node><interface name="{interface}"/><interface name="{PROPERTIES}"/>'
                f'<interface name="{INTROSPECTABLE}"><method name="Introspect"><arg type="s" direction="out"/></method></interface></node>')

    def open(self, url: str) -> None:
        """Through the desktop portal (the session's own browser, whatever this process's environment lacks), else xdg-open."""
        try:
            with open_dbus_connection(bus="SESSION") as connection:
                portal = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop", interface="org.freedesktop.portal.OpenURI")
                answer = connection.send_and_get_reply(new_method_call(portal, "OpenURI", "ssa{sv}", ("", url, {})), timeout=10)
                if answer.header.message_type != MessageType.error:
                    return
                logger.info("the desktop portal refused to open the page: %s", answer.body)
        except (OSError, TimeoutError) as error:
            logger.info("no desktop portal to open the page: %s", error)
        subprocess.Popen(["xdg-open", url], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
