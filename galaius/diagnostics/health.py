"""`galaius doctor`'s health read: one look at this PC and its app, every answer a fact or a finding.

An agent debugging « why does the app show this PC offline » would otherwise read a presence file, a journal, a unit list, a
cgroup and a CI page by hand. Each area here is one small check over a local file or one bounded program (`Run`); the checks
run side by side, so the slowest one is the time spent. A finding is something wrong and names its evidence; a fact is how
things stand. An area that could not be looked at says why (`skipped`) and is never a silent « fine ». No secret is read: the
machine file is read for its server address only, a journal line is masked (`redact`) and clipped before it is shown.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from importlib import metadata
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from galaius.diagnostics.redaction import redact

#: A fact is a scalar: it prints on a line and reads as JSON.
Fact = str | int | float | bool | None
#: Longest a program of a check runs, and the longest the whole look waits for its checks.
PROGRAM_SECONDS = 5.0
LOOK_SECONDS = 8.0
SAID_CHARS = 200
#: The journal lines read for a unit's last words, and for the machine service's story.
JOURNAL_LINES = 40
#: What a CI run on main ending in one of these is a finding (a cancelled run is a newer run's doing, never one).
CI_BAD = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
#: A unit up for less than this that systemd has restarted this many times is in a restart loop, whatever state it shows at the instant looked at.
LOOP_UPTIME = 300.0
LOOP_RESTARTS = 3
#: A spawn waiting for an agent place longer than this is a finding (`EfficiencyLimits.place_wait` for the ones that already started).
PLACE_WAIT = 900.0
#: Shown as « agents memory » when the agents' slice holds this share of its maximum.
MEMORY_SHARE = 0.9


class Unavailable(Exception):
    """A look that could not be made: the words say why (the program missing, too slow, refused)."""


Run = Callable[[list[str], float, tuple[int, ...]], str]


def run_program(argv: list[str], timeout: float, ok: tuple[int, ...] = (0,)) -> str:
    """The standard output of `argv`, bounded by `timeout`; `Unavailable` when it is missing, too slow or exits with a code outside `ok`."""
    if shutil.which(argv[0]) is None:
        raise Unavailable(f"{argv[0]} is not installed")
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        raise Unavailable(f"{argv[0]} did not answer within {timeout:g} s") from None
    except OSError as error:
        raise Unavailable(f"{argv[0]}: {error}") from None
    if done.returncode not in ok:
        raise Unavailable(clip(done.stderr or done.stdout or f"{argv[0]} exited {done.returncode}"))
    return done.stdout


def clip(text: str, limit: int = SAID_CHARS) -> str:
    """`text` masked of credential shapes, on one line, cut to `limit`."""
    flat = " ".join(redact(text).split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def clock(instant: float | None) -> str:
    return "-" if not instant else datetime.fromtimestamp(instant).strftime("%m-%d %H:%M")


def span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h" if seconds < 172800 else f"{seconds / 86400:.1f}d"


class Check(BaseModel):
    """What one area found: how it stands (`facts`), what is wrong (`findings`), or why it was not looked at (`skipped`)."""

    area: str
    facts: dict[str, Fact] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)
    skipped: str = ""


class Finding(BaseModel):
    area: str
    said: str


class Health(BaseModel):
    """The whole look: findings first, then how each area stands, then the areas not looked at. Its exit code is its finding count."""

    findings: list[Finding]
    facts: dict[str, dict[str, Fact]]
    skipped: dict[str, str]
    took_seconds: float

    @classmethod
    def of(cls, checks: list[Check], took: float) -> "Health":
        return cls(findings=[Finding(area=check.area, said=said) for check in checks for said in check.findings],
                   facts={check.area: check.facts for check in checks if check.facts}, skipped={check.area: check.skipped for check in checks if check.skipped},
                   took_seconds=round(took, 2))

    def exit_code(self) -> int:
        return min(len(self.findings), 255)


# --- the machine link ------------------------------------------------------------------------------------------------

#: The machine service's lines that tell how its link to the app fared; the first is the app's answer to a hello.
_STORY = "connected to|server unreachable|server refused|connection closed|connection stopped|machine token|machine file changed"
SERVICE = "galaius-machine.service"


def journal_story(unit: str, run: Run, pattern: str = _STORY, lines: int = JOURNAL_LINES) -> list[tuple[float, str]]:
    """(time, message) of the last `lines` journal entries of the user unit `unit` matching `pattern`, oldest first: read from each entry's
    structured line (`{"ts": …, "message": …}`), the only text taken from it."""
    out = run(["journalctl", "--user", "-u", unit, "-g", pattern, "-n", str(lines), "-o", "short-unix", "--no-pager"], PROGRAM_SECONDS, (0, 1))
    story = []
    for line in out.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            said = json.loads(line[start:]).get("message")
            at = float(line.split(None, 1)[0])
        except (ValueError, AttributeError):
            continue
        if isinstance(said, str):
            story.append((at, said))
    # Oldest first whatever order journalctl printed them in: with `-g` and `-n` it printed the newest first (2026-11-23: the link line read a hello
    # from 00:39 while the service had reconnected at 02:45, the oldest of the 40 entries taken for the last).
    return sorted(story, key=lambda entry: entry[0])


def link(now: float, run: Run, folder: Path | None = None) -> Check:
    """This PC's link to the app: what its tray icon would show, when the link last changed, the app's last answer to a hello and the last trouble."""
    from galaius.machine.presence import PresenceFiles

    files = PresenceFiles(folder=folder) if folder is not None else PresenceFiles.default()
    alive, presence = files.alive(), files.read()
    shown = presence.shown(alive)
    changed = files.path.stat().st_mtime if files.path.exists() else None
    check = Check(area="link", facts={"shown": shown, "pc": presence.pc or None, "connection_version": presence.version or None, "runs": presence.runs,
                                      "update_waiting": presence.update_waiting, "state_written": clock(changed), "state_written_ago": span(now - changed) if changed else None})
    try:
        story = journal_story(SERVICE, run) if sys.platform == "linux" else None
    except Unavailable as error:
        story, check.skipped = None, f"the service's story is unread: {error}"
    hello = next((at for at, said in reversed(story or []) if said.startswith("connected to")), None)
    trouble = next(((at, said) for at, said in reversed(story or []) if not said.startswith("connected to")), None)
    check.facts |= {"last_hello": clock(hello), "last_hello_ago": span(now - hello) if hello else None}
    if trouble is not None:
        check.facts |= {"last_trouble": clock(trouble[0]), "last_trouble_ago": span(now - trouble[0]), "last_trouble_said": clip(trouble[1])}
    if shown in ("connected", "working"):
        return check
    ending = f"; last trouble {span(now - trouble[0])} ago: {clip(trouble[1])}" if trouble else ""
    if shown == "off":
        check.findings.append(f"no connection process holds this PC's link (the app shows it offline): {SERVICE} is not running{ending}")
    elif shown == "refused":
        check.findings.append(f"the app refused this PC's sign-in; run `galaius login`{ending}")
    elif shown == "paused":
        check.findings.append("the link is paused from the tray icon")
    else:
        check.findings.append(f"reconnecting, last hello {span(now - hello) + ' ago' if hello else 'not in the journal'}{ending}")
    return check


# --- the app ---------------------------------------------------------------------------------------------------------


def app(now: float, config_folder: Path | None = None, get: Callable[[str, float], httpx.Response] | None = None) -> Check:
    """The server this PC is enrolled to: its address (from the machine file, which is read for nothing else), whether it answers and which
    build it serves."""
    from galaius.host.paths import UserPaths

    path = (config_folder or UserPaths.config()) / "machine.json"
    try:
        server = str(json.loads(path.read_text(encoding="utf-8")).get("server_url") or "").rstrip("/")
    except (OSError, ValueError, AttributeError):
        server = ""
    if not server:
        return Check(area="app", skipped=f"this PC holds no sign-in ({path.name} unread): run `galaius login`")
    fetch = get or (lambda url, timeout: httpx.get(url, timeout=timeout, follow_redirects=True))
    check, started = Check(area="app", facts={"server": server}), time.monotonic()
    try:
        answer = fetch(f"{server}/v1/version", 3.0)
        body = answer.json() if answer.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as error:
        check.findings.append(f"the app at {server} does not answer: {clip(str(error) or type(error).__name__, 120)}")
        return check
    check.facts |= {"answer_ms": round((time.monotonic() - started) * 1000), "status": answer.status_code, "build_id": body.get("build_id"),
                    "server_version": body.get("version"), "core_version": body.get("core_version")}
    if answer.status_code != 200:
        check.findings.append(f"the app at {server} answers HTTP {answer.status_code} on /v1/version")
    elif (mine := _core_version()) and body.get("core_version") not in (None, mine):
        check.findings.append(f"this PC runs galaius-core {mine}, the app {body.get('core_version')}: run the install line from your Galaius page again")
    return check


def _core_version() -> str | None:
    try:
        return metadata.version("galaius-core")
    except metadata.PackageNotFoundError:
        return None


# --- units -----------------------------------------------------------------------------------------------------------


def units(run: Run, monotonic: float | None = None) -> Check:
    """Each galaius user unit (machine service, run mirror, tunnel, test PC…): a unit that failed or sits in a restart loop is a finding with its last
    journal line. A restart loop is read from the restart counter, never from the state at one instant (a unit crashing every 6 s is `active` in
    its up phase): `auto-restart`, a unit coming back after a restart, or a unit up for under `LOOP_UPTIME` that systemd has restarted
    `LOOP_RESTARTS` times or more. A unit that is inactive and not failed (a timer's one-shot between runs), or one starting for the first
    time, is nothing. `monotonic` is the system's monotonic clock now (systemd stamps units on it)."""
    if sys.platform != "linux":
        return Check(area="units", skipped="user units are systemd's (Linux)")
    try:
        out = run(["systemctl", "--user", "show", "galaius*.service", "-p", "Id,ActiveState,SubState,Result,NRestarts,ExecMainStatus,ActiveEnterTimestampMonotonic"], PROGRAM_SECONDS, (0,))
    except Unavailable as error:
        return Check(area="units", skipped=str(error))
    check, clock_now = Check(area="units"), (time.monotonic() if monotonic is None else monotonic) * 1e6
    for block in out.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name, active, sub = fields.get("Id", ""), fields.get("ActiveState", ""), fields.get("SubState", "")
        if not name:
            continue
        check.facts[name.removesuffix(".service")] = f"{active} ({sub})"
        count = int(fields["NRestarts"]) if fields.get("NRestarts", "").isdigit() else 0
        entered = int(fields["ActiveEnterTimestampMonotonic"]) if fields.get("ActiveEnterTimestampMonotonic", "").isdigit() else 0
        uptime = (clock_now - entered) / 1e6 if entered else None
        restarts = f"{count} restarts, last exit status {fields.get('ExecMainStatus', '?')}"
        if active == "failed":
            what = f"failed (result {fields.get('Result', '?')}, {restarts})"
        elif sub.startswith("auto-restart") or (active == "activating" and count > 0) or (
                active == "active" and count >= LOOP_RESTARTS and uptime is not None and uptime < LOOP_UPTIME):
            what = f"restart loop ({sub}{f', up {span(uptime)}' if active == 'active' and uptime is not None else ''}; {restarts})"
        else:
            continue
        try:
            # The unit's own words: systemd's lines about it (`<unit>: Consumed …`, `Started …`) say nothing of why it ended.
            lines = [line for line in run(["journalctl", "--user", "-u", name, "-n", "30", "-o", "cat", "--no-pager"], PROGRAM_SECONDS, (0,)).splitlines()
                     if line.strip() and not line.startswith((f"{name}:", "Started ", "Stopped ", "Stopping ", "Starting ", "Failed to start"))]
        except Unavailable:
            lines = []
        check.findings.append(f"{name} {what}" + (f"; its last line: {clip(lines[-1])}" if lines else ""))
    return check


# --- agents here -----------------------------------------------------------------------------------------------------


def agents(now: float, folder: Path | None = None, limit: int | None = None) -> Check:
    """Agent runs on this PC against the places it allows, spawns that will not start, the agents' memory slice. Read from the files the
    registry writes (the connection's run count, the spawn line as last written): the registry is not imported, its own
    read of every run record costs seconds (`galaius conversation stats --live` has the exact places)."""
    from galaius.agents.limits.ceiling import slice_cgroup
    from galaius.config.settings import Config
    from galaius.host.paths import UserPaths

    places = limit if limit is not None else Config().agent_runs_max
    check = Check(area="agents", facts={"places_max": places})
    try:
        entries = json.loads(((folder or UserPaths.agents()) / "#spawn-line.state").read_bytes() or b"[]")
    except FileNotFoundError:
        entries = []
    except (OSError, ValueError):
        entries, check.skipped = [], "the spawn line is unreadable"
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    waiting = [entry for entry in entries if entry.get("state") == "waiting"]
    check.facts["waiting"] = len(waiting)
    if waiting:
        oldest = now - min(float(entry.get("asked_at") or now) for entry in waiting)
        check.facts["oldest_wait"] = span(oldest)
        if oldest > PLACE_WAIT:
            check.findings.append(f"{len(waiting)} spawn{'s' if len(waiting) > 1 else ''} waiting for an agent place, the oldest for {span(oldest)} ({places} places)")
    for entry in entries:
        if entry.get("state") == "failed":
            check.findings.append(f"spawn {str(entry.get('run_id', '?'))[:8]} will not start: {clip(str(entry.get('said') or 'no reason kept'))}")
    cgroup = slice_cgroup()
    used = cap = None
    if cgroup is not None:
        used, cap = (_whole(cgroup / name) for name in ("memory.current", "memory.max"))
        check.facts |= {"memory_gib": round(used / 2**30, 1) if used is not None else None, "memory_max_gib": round(cap / 2**30, 1) if cap else None}
    if used is not None and cap and used >= cap * MEMORY_SHARE:
        check.findings.append(f"the agents' memory slice holds {used / 2**30:.1f} of {cap / 2**30:.1f} GiB: no turn starts past its soft limit")
    return check


def _whole(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


# --- efficiency -----------------------------------------------------------------------------------------------------


def efficiency(now: float) -> Check:
    """Whether the agents of the last `EfficiencyLimits.window` spent their time and capability on their task (`galaius.agents.run_efficiency`):
    a start below its role's normal, a bookkeeping share over the limit, a wait for a place, a run ended by a vendor error."""
    from galaius.agents.run_efficiency import EfficiencyLimits, look
    from galaius.agents.run_ledger import RunLedger

    limits = EfficiencyLimits()
    report, found, said = look(RunLedger(), now=now, limits=limits)
    total = report.total
    check = Check(area="efficiency", facts={"window": span(limits.window), "runs": total.runs, "tool_calls": total.calls,
                                            "bookkeeping_share": f"{total.bookkeeping_share():.0%}",
                                            "started_with_less": sum(bool(r.started_with_less) for r in found.runs),
                                            "vendor_ends": sum(bool(r.vendor_end) for r in found.runs)})
    check.findings = said[:limits.listed] + ([f"…and {len(said) - limits.listed} more: `galaius conversation check`"] if len(said) > limits.listed else [])
    return check


# --- limits ----------------------------------------------------------------------------------------------------------


def limits() -> Check:
    """Subscription logins a vendor limit holds back or this PC passes over now (each with when it ends)."""
    from galaius.agents import pc_state

    # a subscription folder this PC cannot read is logged as a warning by the reader: not this look's finding, never its stderr
    quiet = logging.getLogger("galaius.agents.limits.subscriptions")
    before, quiet.level = quiet.level, logging.ERROR
    try:
        logins = pc_state.limits()
    finally:
        quiet.level = before
    check, now = Check(area="limits"), time.time()
    check.facts["logins"] = len(logins)
    for login in logins:
        for pause in login.paused:
            until = datetime.fromisoformat(pause["until"]).timestamp() if pause["until"] else None
            if until is None or until > now:
                check.findings.append(f"{login.provider} login {login.login or login.subscription} paused ({pause['reason']}) until {clock(until)}: {clip(pause['said'], 100)}")
        for block in login.blocked:
            check.findings.append(f"{login.provider} login {login.login or login.subscription} passed over for {block['scope']} until {clock(datetime.fromisoformat(block['until']).timestamp())}")
    return check


# --- CI --------------------------------------------------------------------------------------------------------------


def repository() -> str | None:
    """`owner/name` of the repository this install names as its own (its package metadata's `Repository` link)."""
    try:
        links = metadata.metadata("galaius").get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        return None
    for link_ in links:
        if (found := re.search(r"github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", link_)) and link_.lower().startswith("repository"):
            return found.group(1)
    return None


def ci(now: float, run: Run, slug: str | None = None) -> Check:
    """The newest run of each workflow on the repository's main branch, read through `gh`: one that failed is a finding. Skipped where `gh` is
    missing or not signed in; the look never leaves the PC otherwise."""
    slug = slug or repository()
    if slug is None:
        return Check(area="ci", skipped="this install names no repository")
    try:
        out = run(["gh", "run", "list", "-R", slug, "--branch", "main", "-L", "12", "--json", "workflowName,status,conclusion,headSha,createdAt,url"], PROGRAM_SECONDS, (0,))
        runs = json.loads(out)
    except (Unavailable, ValueError) as error:
        return Check(area="ci", skipped=f"{slug}: {clip(str(error), 120)}")
    newest: dict[str, dict] = {}
    for item in runs:   # newest first
        newest.setdefault(str(item.get("workflowName")), item)
    check = Check(area="ci")
    for name, item in newest.items():
        result = item.get("conclusion") or item.get("status")
        age = now - datetime.fromisoformat(str(item.get("createdAt"))).timestamp()
        check.facts[name] = f"{result} {str(item.get('headSha'))[:7]} {span(age)} ago"
        if item.get("conclusion") in CI_BAD:
            check.findings.append(f"CI {name} on main is {result} at {str(item.get('headSha'))[:7]} ({span(age)} ago): {item.get('url')}")
    return check


# --- the look --------------------------------------------------------------------------------------------------------


def look(run: Run = run_program, now: float | None = None) -> Health:
    """Every area, side by side, never longer than `LOOK_SECONDS`: an area still running then is reported as not looked at, one that raised as a finding."""
    moment = time.time() if now is None else now
    started = time.monotonic()
    areas: dict[str, Callable[[], Check]] = {
        "link": lambda: link(moment, run), "app": lambda: app(moment), "units": lambda: units(run), "agents": lambda: agents(moment),
        "limits": limits, "ci": lambda: ci(moment, run), "efficiency": lambda: efficiency(moment),
    }
    pool = ThreadPoolExecutor(max_workers=len(areas), thread_name_prefix="doctor")
    futures = {name: pool.submit(check) for name, check in areas.items()}
    wait(futures.values(), timeout=LOOK_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)
    checks = []
    for name, future in futures.items():
        if not future.done():
            checks.append(Check(area=name, skipped=f"no answer within {LOOK_SECONDS:g} s"))
        elif (error := future.exception()) is not None:
            checks.append(Check(area=name, findings=[f"the {name} look failed: {clip(f'{type(error).__name__}: {error}', 160)}"]))
        else:
            checks.append(future.result())
    return Health.of(checks, time.monotonic() - started)
