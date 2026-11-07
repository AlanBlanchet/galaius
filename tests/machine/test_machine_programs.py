"""An agent program installed from the web runs only its vendor's own script: fetched over https,
through its vendor's hosts alone, never larger than a script."""

import base64
import os
import shlex
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
from galaius.agents.limits.subscriptions import Subscription, SubscriptionRefused
from galaius_core import AgentSignIn, AgentSignOutRequest, SealedHeld
from galaius_core.identity.sealing import open_with

from galaius.agents.providers import PROVIDERS, AgentProvider, LoginStatus
from galaius.machine.programs import AgentPrograms, ProgramFailed
from galaius.machine.sign_in_browser import SignInHandoff

SCRIPT = b"#!/bin/sh\necho installed\n"


def programs(tmp_path: Path, answer) -> AgentPrograms:
    return AgentPrograms(environment={}, log_path=tmp_path / "agent-programs.log", transport=httpx.MockTransport(answer))


@pytest.mark.parametrize(("hops", "fetched"), [
    ({"https://claude.ai/install.sh": "https://downloads.claude.ai/claude-code-releases/bootstrap.sh"}, True),
    ({"https://claude.ai/install.sh": "https://downloads.example.net/bootstrap.sh"}, False),
    ({"https://claude.ai/install.sh": "http://downloads.claude.ai/bootstrap.sh"}, False),
    ({"https://claude.ai/install.sh": "https://downloads.claude.ai:8443/bootstrap.sh"}, False),
])
def test_the_install_script_comes_only_from_its_vendor(tmp_path: Path, hops: dict[str, str], fetched: bool) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(302, headers={"location": hops[url]}) if url in hops else httpx.Response(200, content=SCRIPT)

    installer = PROVIDERS["claude"].installers["posix"]
    if fetched:
        assert programs(tmp_path, answer).download(installer) == SCRIPT
    else:
        with pytest.raises(ProgramFailed, match="left its vendor") as refused:
            programs(tmp_path, answer).download(installer)
        assert refused.value.refusal == "download_failed"


def test_an_answer_larger_than_any_script_is_refused(tmp_path: Path) -> None:
    huge = programs(tmp_path, lambda request: httpx.Response(200, content=b"x" * (AgentPrograms.script_bytes + 1)))
    with pytest.raises(ProgramFailed, match="larger"):
        huge.download(PROVIDERS["codex"].installers["posix"])


class Broken(Exception):
    pass


def test_a_job_that_breaks_ends_failed_and_a_late_code_never_reopens_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever stops a job (here its version probe and its install both break), its program reads
    `failed`, never stuck `installing`; a device code its sign-in prints afterwards changes nothing,
    and asking again starts a new job."""
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: LoginStatus(signed_in=False))
    monkeypatch.setattr(AgentPrograms, "installable", staticmethod(lambda: (PROVIDERS["claude"],)))
    monkeypatch.setattr(AgentPrograms, "version", lambda self, provider: (_ for _ in ()).throw(Broken("no version")))
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: False)
    started = threading.Event()

    def install(self, provider, key):
        started.wait(5)
        raise Broken("the disk is gone")

    monkeypatch.setattr(AgentPrograms, "_install", install)
    jobs = programs(tmp_path, lambda request: httpx.Response(404))
    assert jobs.install("claude").step == "installing"
    assert jobs.install("claude").step == "installing"  # asked again while it runs: the same job
    started.set()
    deadline = time.monotonic() + 5
    while (state := {item.provider: item for item in jobs.states()}["claude"]).step == "installing" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (state.step, state.failure, state.detail) == ("failed", "install_failed", "Broken: the disk is gone")
    jobs._update(("claude", None), only_while="signing_in", sign_in=AgentSignIn(code="ABCD-12345", expires_at=datetime.now(UTC)))
    assert {item.provider: item for item in jobs.states()}["claude"].sign_in is None


#: What Claude Code 2.1.288 hands `BROWSER` (comes back to this PC) and what it prints for any browser (its page shows a code).
CLAUDE_SIGN_IN = "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=http%3A%2F%2Flocalhost%3A43117%2Fcallback&state=s7"
CLAUDE_PASTE = "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=s7"
#: The code its page shows once accepted, as the stand-in expects it.
CODE = "Zq81vB_x-3kLm0Pa#s7s7s7s7s7"


def stand_in_claude(tmp_path: Path, *, opens: bool, prints: bool, hangs: bool = False) -> Path:
    """`claude auth login` as 2.1.288 runs it with no terminal: `BROWSER` with this PC's address, the address for any
    browser, then « Paste code here if prompted > » (no newline) and one line read: `CODE` signs in, anything else fails
    (« Login failed: Request failed with status code 400 », exit 1)."""
    claude = tmp_path / "claude"
    claude.write_text('#!/bin/sh\n[ "$1" = auth ] || exit 0\n[ "$2" = logout ] && exit 0\n' + ('"$BROWSER" "' + CLAUDE_SIGN_IN + '"\n' if opens else "")
                      + ("echo 'Opening browser to sign in…'\necho \"If the browser didn't open, visit: " + CLAUDE_PASTE + "\"\n"
                         + "printf 'Paste code here if prompted > '\nread code\n"
                         + f'[ "$code" = "{CODE}" ] && {{ touch "$0.in"; echo "Login successful."; sleep 30; }}\n'
                         + ("sleep 30\n" if hangs else "")  # some codes never get an answer
                         + "echo 'Login failed: Request failed with status code 400'; exit 1\n" if prints else "sleep 30\n"))
    claude.chmod(0o755)
    return claude


#: Another key than the sign-in's own (SubjectPublicKeyInfo DER, base64), as a browser would seal to.
OTHER_KEY = base64.b64encode(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
                             .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


def sealer(seal_key: str | None):
    """What his browser does with a code (WebCrypto RSA-OAEP, SHA-256) for the sign-in that published `seal_key`."""
    assert seal_key is not None
    key = serialization.load_der_public_key(base64.b64decode(seal_key))
    return lambda code: base64.b64encode(key.encrypt(code.encode(), AgentPrograms.SEAL)).decode()


def signing(jobs: AgentPrograms, until, seconds: float = 30):  # a loaded PC starts the helper slowly
    deadline = time.monotonic() + seconds
    while not until(state := jobs.states()[0]):
        assert time.monotonic() < deadline, f"stuck at {state.step} {state.sign_in}"
        time.sleep(0.05)
    return state


@pytest.mark.skipif(sys.platform == "win32", reason="its stand-in programs are POSIX shell scripts")
@pytest.mark.parametrize(("opens", "prints", "pasted", "ends"), [
    (True, True, CODE, "ready"),               # his browser anywhere, the code back: signed in, no browser on this PC
    (True, True, "WrongCode_12345#s7s7s7s7", "failed"),  # a code its vendor refuses: code_refused
    (True, False, None, "ready"),              # an older program printing no address for any browser: this PC's own
    (False, False, None, "ready"),             # neither: the terminal command
])
def test_claude_signs_in_from_any_browser_with_the_code_its_page_shows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                                                       opens: bool, prints: bool, pasted: str | None, ends: str) -> None:
    """The sign-in's address for any browser reaches the job (`paste`) and no browser opens on this PC; the code the
    person pastes is written once to the program that printed that address (another attempt's, a second code, a second
    line refused) and never logged. A program printing no such address: the address it handed `BROWSER` (this PC's own),
    else its terminal command."""
    claude = stand_in_claude(tmp_path, opens=opens, prints=prints)
    opened = tmp_path / "opened"
    opener = tmp_path / "opener"
    opener.write_text(f'#!/bin/sh\necho "$1 $BROWSER" > {opened}\n')
    opener.chmod(0o755)
    helper = tmp_path / "galaius-sign-in-browser"  # this install's helper, whatever the test environment installed
    helper.write_text(f'#!/bin/sh\nPYTHONPATH={shlex.quote(os.pathsep.join(sys.path))} exec {shlex.quote(sys.executable)} -m galaius.machine.sign_in_browser "$@"\n')
    helper.chmod(0o755)
    monkeypatch.setattr(SignInHandoff, "helper", staticmethod(lambda: helper))
    by_hand = threading.Event()
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: LoginStatus(signed_in=Path(f"{claude}.in").exists() or by_hand.is_set()))
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.1)
    monkeypatch.setattr(AgentPrograms, "terminal_seconds", 0.5)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: True)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "executable", lambda self: str(claude))
    told = []
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin", "BROWSER": str(opener)}, log_path=tmp_path / "agent-programs.log", signed_in_listeners=[lambda: told.append(True)])
    assert jobs.install("claude").step == "signing_in"
    state = signing(jobs, lambda state: state.sign_in is not None and (state.sign_in.paste is not None if pasted is not None else state.sign_in.link is not None if opens else state.sign_in.command is not None))
    if pasted is not None:
        assert (state.sign_in.link, state.sign_in.paste.pasted) == (CLAUDE_PASTE, False)
        attempt, seal = state.sign_in.paste.attempt, sealer(state.sign_in.paste.seal_key)
        with pytest.raises(SubscriptionRefused, match="no claude sign-in") as refused:
            jobs.paste("claude", UUID(int=7), attempt, seal(pasted))  # another login's: nothing waits there
        assert refused.value.refusal.reason == "sign_in_over"
        with pytest.raises(SubscriptionRefused, match="no claude sign-in") as refused:
            jobs.paste("claude", None, UUID(int=8), seal(pasted))  # another attempt's (an older link)
        assert refused.value.refusal.reason == "sign_in_over"
        with pytest.raises(SubscriptionRefused, match="sealed to this") as refused:
            jobs.paste("claude", None, attempt, sealer(OTHER_KEY)(pasted))  # sealed to another key: whoever carried it cannot swap it
        assert refused.value.refusal.reason == "code_invalid"
        with pytest.raises(SubscriptionRefused, match="not a claude") as refused:
            jobs.paste("claude", None, attempt, seal(pasted + "\nsecond line"))
        assert refused.value.refusal.reason == "code_invalid"
        assert jobs.paste("claude", None, attempt, seal(f" {pasted}\n")).sign_in.paste.pasted  # what a paste carries around it is trimmed
        with pytest.raises(SubscriptionRefused, match="already") as refused:
            jobs.paste("claude", None, attempt, seal(pasted))
        assert refused.value.refusal.reason == "sign_in_over"
    else:
        with pytest.raises(PermissionError):
            jobs.paste("claude", None, UUID(int=8), sealer(OTHER_KEY)(CODE))  # no address for any browser: no code is taken
        if opens:
            assert (state.sign_in.link, state.sign_in.paste) == (CLAUDE_SIGN_IN, None)  # a terminal command may come first under load
        else:
            assert state.sign_in.link is None and state.sign_in.command is not None and state.sign_in.command.endswith("claude auth login")
        by_hand.set()
    state = signing(jobs, lambda state: state.step in ("ready", "failed"))
    assert state.step == ends
    # Owner 2026-11-07 « slash usage … it shows you the usage »: signed in, its usage is asked at once (`_ask_usage` wakes).
    assert told == ([True] if ends == "ready" else [])
    if ends == "failed":
        assert state.failure == "code_refused"
    assert not opened.exists()  # Claude Code's sign-in opens in the person's browser, wherever he is: nothing pops up on this PC
    for secret in ("43117", "Zq81vB", "WrongCode"):
        assert secret not in caplog.text
        assert not jobs.log_path.exists() or secret not in jobs.log_path.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="its stand-in program is a POSIX shell script")
def test_a_part_of_the_code_is_refused_and_a_code_never_answered_ends_the_sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code given only the part before `#` says « Invalid code » and waits for another: such a code never reaches
    it (he can paste the whole one); a code it never answers ends the sign-in after `code_seconds`, never 15 minutes."""
    claude = stand_in_claude(tmp_path, opens=False, prints=True, hangs=True)
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: LoginStatus(signed_in=False))
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.1)
    monkeypatch.setattr(AgentPrograms, "code_seconds", 1)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: True)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "executable", lambda self: str(claude))
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "agent-programs.log")
    assert jobs.install("claude").step == "signing_in"
    state = signing(jobs, lambda state: state.sign_in is not None and state.sign_in.paste is not None)
    attempt, seal = state.sign_in.paste.attempt, sealer(state.sign_in.paste.seal_key)
    with pytest.raises(PermissionError, match="whole code"):
        jobs.paste("claude", None, attempt, seal("Zq81vB_x-3kLm0Pa"))
    assert jobs.paste("claude", None, attempt, seal("Zq81vB_x-3kLm0Pa#other_state")).sign_in.paste.pasted  # still waiting: the whole one goes
    state = signing(jobs, lambda state: state.step != "signing_in", seconds=15)
    assert (state.step, state.failure) == ("failed", "code_refused")


@pytest.mark.skipif(sys.platform == "win32", reason="its stand-in program is a POSIX shell script")
def test_signing_a_subscription_out_ends_its_sign_in_under_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subscription removed while its sign-in waits for his code (he never pasted it): the sign-in is ended, then
    signed out and its folder removed, at once, never « sign out once it ended » for the sign-in's 15 minutes."""
    claude = stand_in_claude(tmp_path, opens=False, prints=True)
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: LoginStatus(signed_in=False))
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.1)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: True)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "executable", lambda self: str(claude))
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "agent-programs.log")
    subscription = UUID(int=42)
    assert jobs.install("claude", subscription).step == "signing_in"
    signing(jobs, lambda state: any(item.subscription_id == subscription and item.sign_in is not None and item.sign_in.paste is not None for item in jobs.states()))
    started = time.monotonic()
    assert jobs.sign_out("claude", subscription).step == "signed_out"
    assert time.monotonic() - started < 10
    assert not any(item.subscription_id == subscription for item in jobs.states())  # its folder is gone


def test_a_sign_out_waits_long_enough_for_an_ended_sign_in_to_be_seen_and_for_the_web_to_hear_back() -> None:
    """Its sign-in's watcher sees the program end only at its next poll, at worst two polls late; the web waits for all of it."""
    assert AgentSignOutRequest.stop_seconds >= 2 * AgentPrograms.poll_seconds + 10
    assert AgentSignOutRequest.seconds > AgentSignOutRequest.settle_seconds + AgentSignOutRequest.stop_seconds + AgentSignOutRequest.logout_seconds


@pytest.mark.skipif(sys.platform == "win32", reason="its stand-in program is a POSIX shell script")
def test_a_subscription_signed_out_while_its_program_installs_waits_for_the_install_then_signs_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-10-30 « stop placing hard rules »: signed out mid-install it was refused (« sign out once it is installed »); it
    now waits for the install, ends the sign-in that follows, and signs out."""
    claude = stand_in_claude(tmp_path, opens=False, prints=True)
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: LoginStatus(signed_in=False))
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.1)
    monkeypatch.setattr(AgentPrograms, "_install", lambda self, provider, key: (self._update(key, step="installing"), time.sleep(0.5)))
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: False)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "executable", lambda self: str(claude))
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "agent-programs.log")
    subscription = UUID(int=43)
    jobs.install("claude", subscription)
    signing(jobs, lambda state: any(item.subscription_id == subscription and item.step == "installing" for item in jobs.states()))
    assert jobs.sign_out("claude", subscription).step == "signed_out"
    assert not any(item.subscription_id == subscription for item in jobs.states())


#: Claude Code 2.1.288 with its claude.ai login expired and not refreshable: the JSON status still says
#: `loggedIn: true`, the text status says « Login: Expired » and exits 1. `old` is a Claude Code without
#: `--text` (its own refusal, exit 1); `slow` answers neither in time.
EXPIRED_CLAUDE = """#!/bin/sh
[ -e "$0.slow" ] && sleep 30
[ "$1 $2" = "auth status" ] || exit 0
if [ "$3" = --text ]; then
  [ -e "$0.old" ] && { echo "error: unknown option '--text'" >&2; exit 1; }
  [ -e "$0.expired" ] && { echo "Login: Expired — log in again"; echo "Not logged in. Run claude auth login to authenticate."; exit 1; }
  echo "Email: alan@example.com"; exit 0
fi
echo '{"loggedIn": true, "authMethod": "claude.ai", "email": "alan@example.com", "subscriptionType": "max"}'
"""


@pytest.mark.skipif(sys.platform == "win32", reason="its stand-in program is a POSIX shell script")
def test_an_expired_login_reads_signed_out_never_ready_and_a_slow_check_keeps_the_last_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude installed, its login expired: signed out with `expired` (installed, so « Se reconnecter »,
    never an install), never ready; signed in again: ready, also on a Claude Code too old for the text
    status (its refusal is no expiry). A status check that cannot answer in time on a busy PC keeps that
    last answer instead of reading signed out."""
    claude = tmp_path / "claude"
    claude.write_text(EXPIRED_CLAUDE)
    claude.chmod(0o755)
    (tmp_path / "claude.expired").touch()
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: True)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "executable", lambda self: str(claude))
    monkeypatch.setattr(AgentPrograms, "installable", staticmethod(lambda: (PROVIDERS["claude"],)))
    monkeypatch.setattr(AgentPrograms, "version", lambda self, provider: "2.1.288 (Claude Code)")
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "agent-programs.log")
    expired = jobs.states()[0]
    assert (expired.step, expired.available, expired.signed_in, expired.expired) == ("signed_out", True, False, True)
    (tmp_path / "claude.expired").unlink()
    ready = jobs.states()[0]
    assert (ready.step, ready.signed_in, ready.expired, ready.account) == ("ready", True, False, "alan@example.com")
    (tmp_path / "claude.old").touch()
    assert jobs.states()[0].step == "ready"
    (tmp_path / "claude.slow").touch()
    asked = AgentProvider.login_status
    monkeypatch.setattr(AgentProvider, "login_status", lambda self, env, timeout=10: asked(self, env, timeout=0.3))
    assert jobs.states()[0].step == "ready"


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script stands in for the CLI")
def test_a_programs_version_is_asked_again_only_once_its_executable_changed(tmp_path: Path) -> None:
    """Each look at the programs ran every login's CLI for `--version` (15 at a time, 2026-11-03)."""
    asked, cli = tmp_path / "asked", tmp_path / "cli"
    cli.write_text(f"#!/bin/sh\necho x >> {shlex.quote(str(asked))}\necho '2.1.288 (Claude Code)'\n")
    cli.chmod(0o755)

    class Located:
        def executable(self) -> str:
            return str(cli)

    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "agent-programs.log")
    assert jobs.version(Located()) == jobs.version(Located()) == "2.1.288 (Claude Code)"
    assert asked.read_text().count("x") == 1
    update = tmp_path / "cli.new"   # an update puts a new file in its place
    update.write_text(cli.read_text().replace("2.1.288", "2.1.300"))
    update.chmod(0o755)
    update.replace(cli)
    assert jobs.version(Located()) == "2.1.300 (Claude Code)" and asked.read_text().count("x") == 2


#: A held login's token, as `claude setup-token` prints it.
HELD_TOKEN = "sk-ant-oat01-" + "Hq7_x" * 20


def stand_in_setup_token(tmp_path: Path) -> Path:
    """`claude setup-token` as 2.1.288 draws it, only under a terminal: words spaced by cursor moves, the address inside an
    OSC-8 link and again as its text, « Paste code here if prompted > »; `CODE` then prints the token; another code
    « OAuth error: … 400 » and « Press Enter to retry », waiting."""
    claude = tmp_path / "claude"
    address = CLAUDE_PASTE.replace("%", "%%")  # printf's own
    link = f"\\033]8;id=x;{address}\\007\\033[38;5;246m{address}\\033[39m\\033]8;;\\007"
    claude.write_text("#!/bin/sh\n[ \"$1\" = setup-token ] || exit 0\n[ -t 0 ] && [ -t 1 ] || { echo 'needs a terminal'; exit 3; }\n"
                      f"printf '\\033[2GBrowser\\033[10Gdidn'\"'\"'t\\033[17Gopen?\\r\\r\\n\\r\\r\\n{link}\\r\\r\\n\\033[2GPaste\\033[8Gcode\\033[13Ghere\\033[18Gif\\033[21Gprompted\\033[30G>\\r\\r\\n'\n"
                      "IFS= read -r code\n"
                      f"[ \"$code\" = \"{CODE}\" ] && {{ printf '\\033[2GYour\\033[7GOAuth\\033[13Gtoken\\033[19G(valid\\033[26Gfor\\033[30G1\\033[32Gyear)\\r\\r\\n\\r\\r\\n\\033[2G{HELD_TOKEN}\\r\\r\\n'; sleep 30; }}\n"
                      "printf '\\033[38;5;211mOAuth error: Request failed with status code 400\\033[39m\\r\\nPress Enter to retry.\\r\\n'; sleep 30\n")
    claude.chmod(0o755)
    return claude


def opened(private: rsa.RSAPrivateKey, sealed: SealedHeld | None) -> str:
    """What the platform opens a `held_take` answer to."""
    assert sealed is not None
    return open_with(private, sealed)


def seal_key() -> tuple[rsa.RSAPrivateKey, str]:
    """The platform's one-request key and its public half as `AgentHeldTakeRequest.seal_key`."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


@pytest.mark.skipif(sys.platform == "win32", reason="a held sign-in runs under a POSIX terminal")
@pytest.mark.parametrize(("pasted", "signs", "ends"), [(CODE, True, "ready"), ("WrongCode_12345#s7s7s7s7", True, "code_refused"),
                                                       (CODE, False, "sign_in_failed")])  # a token signing nothing in: removed again
def test_a_held_sign_in_keeps_its_token_in_the_folder_alone_then_hands_it_only_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                                                                     pasted: str, signs: bool, ends: str) -> None:
    """Its held command under a terminal: the address reaches the web, the code its page shows goes back once, the token
    it prints lands in the folder (0600) and nowhere else (no log, no state, no answer); `held_take` hands it sealed to the
    request's key alone, `held_put` puts it on another folder once (the same token again writes nothing). A refused code
    ends the sign-in at once; the default login holds none."""
    claude = stand_in_setup_token(tmp_path)
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: LoginStatus(
        signed_in=signs and environment.get("CLAUDE_CODE_OAUTH_TOKEN") == HELD_TOKEN, login="9" * 16 if environment.get("CLAUDE_CODE_OAUTH_TOKEN") else ""))
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.1)
    monkeypatch.setattr(AgentPrograms, "code_seconds", 60)  # a refused code ends on its words, never on this wait
    monkeypatch.setattr(type(PROVIDERS["claude"]), "available", lambda self: True)
    monkeypatch.setattr(type(PROVIDERS["claude"]), "executable", lambda self: str(claude))
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "agent-programs.log")
    assert jobs.install("claude", None, held=True).failure == "held_unsupported"
    subscription = UUID(int=42)
    with caplog.at_level("DEBUG"):
        assert jobs.install("claude", subscription, held=True).step == "signing_in"
        mine = lambda: next(item for item in jobs.states() if item.subscription_id == subscription)
        signing(jobs, lambda _state: (item := mine()).sign_in is not None and item.sign_in.paste is not None)
        state = mine()
        assert state.sign_in.link == CLAUDE_PASTE
        jobs.paste("claude", subscription, state.sign_in.paste.attempt, sealer(state.sign_in.paste.seal_key)(pasted))
        started = time.monotonic()
        signing(jobs, lambda _state: mine().step in ("ready", "failed"))
        state = mine()
        assert (state.step if ends == "ready" else state.failure) == ends and time.monotonic() - started < 15
        held = Subscription.of(subscription)
        if ends != "ready":
            assert held.held is None and jobs.held_take("claude", subscription, OTHER_KEY) is None
            return
        assert held.held == HELD_TOKEN and (held.folder / "held-token").stat().st_mode & 0o777 == 0o600
        key, public = seal_key()
        assert opened(key, jobs.held_take("claude", subscription, public)) == HELD_TOKEN
        assert jobs.held_take("claude", UUID(int=43), public) is None  # no folder here: none held
        put = jobs.held_put("claude", UUID(int=43), HELD_TOKEN)
        written = (Subscription.of(UUID(int=43)).folder / "held-token").stat().st_mtime_ns
        assert put.step == "ready" and jobs.held_put("claude", UUID(int=43), HELD_TOKEN).step == "ready"
        assert (Subscription.of(UUID(int=43)).folder / "held-token").stat().st_mtime_ns == written
        with pytest.raises(PermissionError):
            jobs.held_put("claude", UUID(int=44), HELD_TOKEN + "\nOTHER=1")
        shown = "".join(item.model_dump_json() for item in jobs.states())
    for seen in (caplog.text, shown, jobs.log_path.read_text() if jobs.log_path.exists() else ""):
        assert HELD_TOKEN not in seen
    assert jobs.sign_out("claude", subscription).step == "signed_out" and not held.folder.exists()  # no vendor logout: the folder alone


#: A held Codex login's file, as `codex login --device-auth` leaves it.
HELD_FILE = '{"auth_mode":"chatgpt","tokens":{"access_token":"' + "Ac7_x" * 20 + '","refresh_token":"' + "Rf7_x" * 20 + '"}}'


@pytest.mark.skipif(sys.platform == "win32", reason="a held sign-in runs on POSIX")
def test_a_held_login_file_is_the_folders_own_and_moves_only_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A program reading its held login from a file (Codex `auth.json`): its sign-in runs on pipes, its device code
    reaches the web, the file it leaves in the folder is the held login; `held_take` hands it sealed, `held_put` writes
    it into another folder (0600, one step; the same file again writes nothing; never through a link, never what is not
    a JSON object), and no launch environment carries it."""
    codex = tmp_path / "codex"
    codex.write_text('#!/bin/sh\n[ "$1 $2" = "login --device-auth" ] || exit 0\n[ -t 0 ] && { echo "a terminal"; exit 3; }\necho ABCD-EFGH\nsleep 1\n'
                     f"umask 077; printf '%s' '{HELD_FILE}' > \"$CODEX_HOME/auth.json\"\nsleep 30\n")
    codex.chmod(0o755)
    monkeypatch.setattr(AgentPrograms, "_status", lambda self, provider, environment: LoginStatus(
        signed_in=(Path(environment.get("CODEX_HOME", "/nonexistent")) / "auth.json").exists(), login="8" * 16))
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.1)
    monkeypatch.setattr(type(PROVIDERS["codex"]), "available", lambda self: True)
    monkeypatch.setattr(type(PROVIDERS["codex"]), "executable", lambda self: str(codex))
    jobs = AgentPrograms(environment={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "agent-programs.log")
    subscription = UUID(int=52)
    with caplog.at_level("DEBUG"):
        assert jobs.install("codex", subscription, held=True).step == "signing_in"
        mine = lambda: next(item for item in jobs.states() if item.subscription_id == subscription)
        signing(jobs, lambda _state: mine().step in ("ready", "failed") or (mine().sign_in is not None and mine().sign_in.code is not None))
        assert mine().step == "ready" or mine().sign_in.code == "ABCD-EFGH"
        signing(jobs, lambda _state: mine().step in ("ready", "failed"))
        assert mine().step == "ready"
        held = Subscription.of(subscription)
        assert held.held == HELD_FILE
        key, public = seal_key()
        assert opened(key, jobs.held_take("codex", subscription, public)) == HELD_FILE
        other = UUID(int=53)
        assert jobs.held_put("codex", other, HELD_FILE).step == "ready"
        path = Subscription.of(other).folder / "auth.json"
        written = path.stat().st_mtime_ns
        assert path.read_text() == HELD_FILE and path.stat().st_mode & 0o777 == 0o600
        jobs.held_put("codex", other, HELD_FILE)
        assert path.stat().st_mtime_ns == written
        with pytest.raises(PermissionError):
            jobs.held_put("codex", other, "not json")
        launched = PROVIDERS["codex"].child_environment({"PATH": os.defpath, **Subscription.of(other).overlay()}, mcp_wait_seconds=0)
        assert not any(HELD_FILE in value or "Ac7_x" in value for value in launched.values())
        path.unlink()
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("{}")
        elsewhere.chmod(0o600)
        path.symlink_to(elsewhere)
        assert Subscription.of(other).held is None
        with pytest.raises(PermissionError, match="link"):
            jobs.held_put("codex", other, HELD_FILE)
        assert elsewhere.read_text() == "{}"
        shown = "".join(item.model_dump_json() for item in jobs.states())
    for seen in (caplog.text, shown, jobs.log_path.read_text() if jobs.log_path.exists() else ""):
        assert "Ac7_x" not in seen
