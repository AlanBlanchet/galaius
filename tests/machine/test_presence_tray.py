"""Invariant: the tray icon says what this computer's connection really is (owner 2026-11-14 « c'est toujours pas
stable le fait qu'un PC soit connecté. Il n'y a pas de barre de tâches ou quoi que ce soit en bas à droite »):
every state × its menu, the server's silence turning « connecté » into « reconnexion », a dead connection read as
« éteint » from its lock alone, a deploy or a sleep never leaving a PC away longer than the retry it needs."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import serve

from galaius.machine.connection import MachineConfig, MachineRunner, Paused, Woke
from galaius.machine.presence import Presence, PresenceFiles, PresenceWatch
from galaius.machine.tray.app import PlatformTray
from galaius.machine.tray.view import TrayPicture, TrayView, TrayWords

if sys.platform == "linux":
    from jeepney import DBusAddress, MessageType, new_method_call

    from galaius.machine.tray.linux import ITEM, MENU, PROPERTIES, StatusNotifierTray

FR = TrayWords.by_language["fr"]
PAGE = "https://galaius.example.org/plateform/#data?computer=abc"


@pytest.mark.parametrize(("presence", "alive", "shown", "first", "actions"), [
    (Presence(link="connected", pc="pc", page=PAGE), True, "connected", "pc · Connecté", ["open", "pause", "quit"]),
    (Presence(link="connected", runs=1, pc="pc", page=PAGE), True, "working", "pc · 1 agent actif", ["open", "pause", "quit"]),
    (Presence(link="connected", runs=12, pc="pc", page=PAGE, update_waiting=True), True, "working", "pc · 12 agents actifs", ["open", "pause", None, "quit"]),
    (Presence(link="reconnecting", runs=3, pc="pc", page=PAGE), True, "reconnecting", "pc · Reconnexion…", ["open", "pause", "quit"]),
    (Presence(link="paused", pc="pc", page=PAGE), True, "paused", "pc · En pause", ["open", "resume", "quit"]),
    (Presence(link="connected", runs=4, pc="pc", page=PAGE, update_waiting=True), False, "off", "pc · Éteint", ["open", "start", "quit"]),
    (Presence(link="refused", pc="pc", page=PAGE), False, "refused", "pc · À reconnecter", ["open", "quit"]),
    (Presence(), True, "reconnecting", "Reconnexion…", ["pause", "quit"]),  # nothing written yet: no page to open, no name
    (Presence(), False, "off", "Éteint", ["start", "quit"]),
])
def test_each_state_shows_its_word_and_only_the_rows_it_can_do(presence: Presence, alive: bool, shown: str, first: str, actions: list) -> None:
    view = TrayView.of(presence, alive, FR, frame=7)
    assert view.shown == shown and view.items[0].label == first and view.items[0].action is None
    assert [item.action for item in view.items[1:]] == actions
    assert view.tooltip == "Galaius · " + first.split(" · ")[-1]
    assert view.frame == (7 % TrayView.frames if shown == "working" else 0), "only working moves"


@pytest.mark.parametrize(("link", "heard_ago", "paused", "expected"), [
    ("connected", 0.0, False, "connected"),
    ("connected", PresenceWatch.heard_within - 0.1, False, "connected"),
    ("connected", PresenceWatch.heard_within + 0.1, False, "reconnecting"),  # two beats unanswered
    ("connected", None, False, "reconnecting"),
    ("reconnecting", 0.0, False, "reconnecting"),
    ("connected", 0.0, True, "paused"),
    ("refused", None, False, "refused"),
])
def test_the_server_answering_is_what_connected_means(tmp_path: Path, link: str, heard_ago: float | None, paused: bool, expected: str) -> None:
    watch = PresenceWatch(files=PresenceFiles(folder=tmp_path), link=link, heard=None if heard_ago is None else 1000.0 - heard_ago)
    watch.files.pause(paused)
    assert watch.presence(runs=0, update_waiting=False, pc="pc", page="", version="v", now=1000.0).link == expected


@pytest.mark.parametrize("content", [None, b"", b'{"link": "connec', b'{"link": "elsewhere"}', b"\xff\xfe"])
def test_an_absent_half_written_or_foreign_file_reads_as_reconnecting(tmp_path: Path, content: bytes | None) -> None:
    files = PresenceFiles(folder=tmp_path)
    if content is not None:
        files.path.write_bytes(content)
        files.path.chmod(0o600)
    assert files.read() == Presence()


def test_off_is_read_from_the_connections_lock_never_from_a_clock(tmp_path: Path) -> None:
    files = PresenceFiles(folder=tmp_path)
    assert not files.alive()
    holder = subprocess.Popen([sys.executable, "-c", "import sys,time; from pathlib import Path; from galaius.machine.presence import PresenceFiles;"
                               "PresenceFiles(folder=Path(sys.argv[1])).hold(); print('held', flush=True); time.sleep(60)", str(tmp_path)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert files.alive() and files.alive(), "looking frees the lock at once: a second look still sees it held"
    finally:
        holder.kill()
        holder.wait()
    assert not files.alive(), "a killed connection is off at the next look"


@pytest.mark.parametrize(("failures", "restart_age", "low", "high"), [
    (0, None, 1, 1), (3, None, 10, 10), (40, None, 20, 20),          # an ordinary drop: the backoff
    (6, 0.0, 2, 3), (40, 179.0, 2, 3),                               # the server said it restarts: every 2-3 s
    (6, 181.0, 20, 20),                                              # past the window: the backoff again
])
def test_a_restarting_server_is_tried_every_few_seconds_then_backed_off(failures: int, restart_age: float | None, low: float, high: float) -> None:
    runner = MachineRunner.__new__(MachineRunner)
    delays = [runner.retry_delay(failures, None if restart_age is None else 1000.0 - restart_age, now=1000.0) for _ in range(50)]
    assert low <= min(delays) and max(delays) <= high


@pytest.mark.parametrize(("monotonic", "wall", "slept"), [(3.0, 3.0, False), (12.9, 3.0, False), (3.0, 14.0, True), (300.0, 3.0, True), (3.0, -3600.0, False)])
def test_a_wait_that_lasted_a_sleep_is_seen_by_either_clock(monkeypatch: pytest.MonkeyPatch, monotonic: float, wall: float, slept: bool) -> None:
    """Linux and macOS stop the monotonic clock while asleep, Windows does not: either one running long means a sleep;
    a wall clock set back an hour is no sleep."""
    runner = MachineRunner.__new__(MachineRunner)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0 + monotonic)
    monkeypatch.setattr(time, "time", lambda: 5000.0 + wall)
    assert runner.slept(100.0, 5000.0, 3.0) is slept


@pytest.mark.parametrize("ending", ["paused", "woke"])
def test_beats_are_answered_and_a_pause_or_a_sleep_leaves_the_link(tmp_path: Path, ending: str) -> None:
    """Over a real websocket: each beat's ping answered marks the server heard; the pause file, or a beat that took a
    sleep long, ends the connection with the reason the reconnect loop acts on."""
    runner = MachineRunner(tmp_path / "machine.json")
    runner.heartbeat_seconds = 0.05
    runner._beat = lambda config: {}
    config = MachineConfig(server_url="http://127.0.0.1:1", workspace_id="00000000-0000-0000-0000-000000000001", machine_id="00000000-0000-0000-0000-000000000002",
                           token="t" * 40, permission_ceiling="read_only", working_directory=tmp_path)

    async def scenario() -> None:
        async def echo(connection) -> None:
            async for _ in connection:
                pass
        async with serve(echo, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                beating = asyncio.create_task(runner._heartbeat(socket, config))
                await asyncio.sleep(0.3)
                assert runner.presence.heard is not None and time.monotonic() - runner.presence.heard < 0.2
                if ending == "paused":
                    runner.presence.files.pause(True)
                else:
                    runner.woke_after_seconds = -1.0
                with pytest.raises(Paused if ending == "paused" else Woke):
                    await asyncio.wait_for(beating, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("shown", list(TrayPicture.dots))
@pytest.mark.parametrize("size", [16, 22, 24, 32, 64])
def test_every_state_draws_its_mark_and_dot_at_every_size(shown: str, size: int) -> None:
    picture = TrayPicture.drawn(shown, 2, size)
    assert picture.size == (size, size) and picture.mode == "RGBA"
    dot = picture.getpixel((round(49 / 64 * size), round(49 / 64 * size)))
    assert max(abs(a - b) for a, b in zip(dot[:3], TrayPicture.dots[shown])) <= 40 and dot[3] > 200, "the dot's centre is the state's colour"
    pupil = picture.getpixel((size // 2, size // 2))[3]
    assert (pupil < 160) if shown == "off" else (pupil > 200), "off fades the mark"
    argb = TrayPicture.argb(shown, 2, size)
    assert len(argb) == 4 * size * size and argb[0::4] == picture.getchannel("A").tobytes()


@pytest.mark.skipif(sys.platform != "linux", reason="the D-Bus StatusNotifierItem is Linux's")
def test_the_panel_host_reads_the_icon_and_menu_and_a_click_reaches_its_action() -> None:
    acted: list[str] = []
    tray = StatusNotifierTray()
    tray.driver = type("Driver", (), {"leaving": False, "act": lambda self, action: acted.append(action), "tick": lambda self: None})()
    tray.view = TrayView.of(Presence(link="connected", runs=2, pc="pc", page=PAGE), True, FR)

    def call(path: str, interface: str, member: str, signature: str | None = None, body: tuple = ()):
        answer = tray.reply(new_method_call(DBusAddress(path, bus_name="x.y", interface=interface), member, signature, body), path, interface, member)
        return answer.header.message_type, answer.body

    kind, (properties,) = call(tray.item_path, PROPERTIES, "GetAll", "s", (ITEM,))
    assert kind == MessageType.method_return and properties["Menu"] == ("o", tray.menu_path) and properties["ItemIsMenu"] == ("b", True)
    assert [width for width, _, pixels in properties["IconPixmap"][1] if len(pixels) == 4 * width * width] == list(tray.sizes)
    assert properties["ToolTip"][1][3] == "Galaius · 2 agents actifs"
    kind, (revision, (root, _, children)) = call(tray.menu_path, MENU, "GetLayout", "iias", (0, -1, []))
    labels = [child[1][1]["label"][1] for child in children]
    assert root == 0 and labels == ["pc · 2 agents actifs", "Ouvrir Galaius", "Mettre en pause", "Quitter"]
    assert [child[1][1]["enabled"][1] for child in children] == [False, True, True, True]
    for index in range(0, len(children) + 2):  # the header, a row, the root, past the end
        call(tray.menu_path, MENU, "Event", "isvu", (index, "clicked", ("s", ""), 0))
    assert acted == ["open", "pause", "quit"], "only rows with an action act, once each"
    assert call("/elsewhere", PROPERTIES, "GetAll", "s", (ITEM,))[0] == MessageType.error


def test_this_systems_tray_shows_the_icon_then_leaves_when_asked() -> None:
    """The real tray of the system it runs on (Windows notification area, macOS menu bar, a Linux StatusNotifierItem
    host): the icon is up within a few ticks, and the backend returns once its driver leaves."""
    if sys.platform == "linux" and subprocess.run(["busctl", "--user", "status", "org.kde.StatusNotifierWatcher"], capture_output=True).returncode != 0:
        pytest.skip("no StatusNotifierItem host in this session")
    backend = PlatformTray()
    seen: list[bool] = []

    class Driver:
        leaving, ticks = False, 0

        def tick(self) -> TrayView | None:
            self.ticks += 1
            if self.ticks == 4:
                seen.append(visible(backend))
                self.leaving = True
            return TrayView.of(Presence(link="connected", runs=1, pc="lab", page=PAGE), True, FR, frame=self.ticks)

        def act(self, action: str) -> None: ...

    def visible(tray) -> bool:
        if sys.platform == "win32":
            return tray.added
        if sys.platform == "darwin":
            return tray.item.button().image() is not None
        listed = subprocess.run(["busctl", "--user", "get-property", "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                                 "org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems"], capture_output=True, text=True).stdout
        return tray.name in listed

    started = time.monotonic()
    backend.run(Driver())
    assert seen == [True] and time.monotonic() - started < 10
