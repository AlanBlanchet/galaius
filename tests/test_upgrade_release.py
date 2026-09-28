"""A release is believed only when a shipped key signed it, installed only when newer than anything
this computer ran, and the upgrade settings are this computer's alone."""

import hashlib
import http.server
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from interact.machines import MachineConfig, MachineRunner
from interact.server_tool_settings import PORTABLE_ENV
from interact.config.settings import Config
from interact.upgrade.check import UpgradeCheck
from interact.upgrade.release import BuildIdentity, Release, ReleaseFile, ReleaseRefused
from interact.upgrade.source import ReleaseSigner, ReleaseSource
from interact.upgrade.store import RuntimeReceipt, RuntimeStore
from interact.upgrade.supervisor import Supervision, Supervisor

WHEEL = b"not really a wheel"
LOCK = b"# nothing\n"


def release(version: str, minutes: int, **changes) -> Release:
    at = datetime(2026, 9, 28, tzinfo=UTC) + timedelta(minutes=minutes)
    wheels = tuple(ReleaseFile(name=name, version=version, sha256=hashlib.sha256(WHEEL).hexdigest(), file=f"{name.replace('-', '_')}-{version}-py3-none-any.whl")
                   for name in Release.packages)
    values = {"version": version, "released_at": at, "commit": f"{minutes:07x}", "expires_at": at + timedelta(days=60), "wheels": wheels,
              "lock": ReleaseFile(name="requirements.lock", sha256=hashlib.sha256(LOCK).hexdigest(), file="requirements.lock")}
    return Release(**{**values, **changes})


class Published:
    """A loopback release server whose document can be swapped mid-test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        handler = type("Handler", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *_: None})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: handler(*args, directory=str(root)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.source = ReleaseSource(base=f"http://127.0.0.1:{self.server.server_port}", kind="server")

    def publish(self, signer: ReleaseSigner, document: Release, *, signature: bytes | None = None, tamper: bool = False) -> None:
        body, signed = signer.sign(document)
        (self.root / "release.json").write_bytes(body.replace(b'"version": "0.44.0"', b'"version": "9.44.0"', 1) if tamper else body)
        (self.root / "release.json.sig").write_bytes(signature or signed)


@pytest.fixture
def published(tmp_path: Path):
    folder = tmp_path / "published"
    folder.mkdir()
    served = Published(folder)
    yield served
    served.server.shutdown()


@pytest.fixture
def signer() -> ReleaseSigner:
    return ReleaseSigner.generated()


def settings(**changes) -> Config:
    return Config(**{"auto_upgrade": True, "upgrade_pin": "", "upgrade_check_seconds": 300, "upgrade_github": False, **changes})


def checker(tmp_path: Path, signer: ReleaseSigner, published: Published, monkeypatch, **changes) -> UpgradeCheck:
    check = UpgradeCheck(store=RuntimeStore(root=tmp_path / "runtimes"), keys=signer.keys(), config=settings(**changes))
    monkeypatch.setattr(UpgradeCheck, "source", lambda self: published.source)
    monkeypatch.setattr(UpgradeCheck, "install", lambda self, *args: pytest.fail("nothing may be installed"))
    return check


@pytest.mark.parametrize("forged", ["other key", "tampered document", "no signature"])
def test_a_release_not_signed_by_a_trusted_key_is_refused_before_anything_is_fetched(tmp_path, signer, published, monkeypatch, forged) -> None:
    document = release("0.44.0", 10, commit="0a0b0c0")
    if forged == "other key":
        published.publish(ReleaseSigner.generated(), document)
    else:
        published.publish(signer, document, tamper=forged == "tampered document", signature=b"\n" if forged == "no signature" else None)
    check = checker(tmp_path, signer, published, monkeypatch)
    check.run()
    assert check.store.events()[-1].kind == "refused"
    assert check.store.pointer().active is None


def test_a_file_whose_bytes_differ_from_the_signed_sha256_is_refused() -> None:
    wheel = release("0.44.0", 10).wheel("interact")
    assert wheel.check(WHEEL) == WHEEL
    with pytest.raises(ReleaseRefused, match="sha256 differs"):
        wheel.check(WHEEL + b"!")


def test_an_older_release_than_this_computer_ran_is_refused_and_said_once(tmp_path, signer, published, monkeypatch) -> None:
    check = checker(tmp_path, signer, published, monkeypatch)
    check.store._update(lambda pointer: pointer.model_copy(update={"floor": release("0.44.0", 20).order}))
    published.publish(signer, release("0.44.0", 10))  # same version, older commit time
    assert "older than" in check.run()
    assert "older than" in check.run()
    assert [event.kind for event in check.store.events()] == ["refused"]


@pytest.mark.parametrize(("changes", "said"), [({"auto_upgrade": False}, "off"), ({"upgrade_pin": "abcdef0"}, "holding the pinned build abcdef0")])
def test_off_and_pinned_install_nothing_newer(tmp_path, signer, published, monkeypatch, changes, said) -> None:
    published.publish(signer, release("0.44.0", 30))
    assert said in checker(tmp_path, signer, published, monkeypatch, **changes).run()


def test_a_pin_that_is_not_a_commit_is_reported_and_ignored_never_fatal(tmp_path, signer, published, monkeypatch) -> None:
    published.publish(signer, release("0.44.0", 30))
    check = checker(tmp_path, signer, published, monkeypatch, upgrade_pin="0.44.0")
    monkeypatch.setattr(UpgradeCheck, "install", lambda self, *args: (_ for _ in ()).throw(RuntimeError("reached install")))
    assert "did not install: reached install" in check.run()  # the bad pin did not hold anything back
    assert any("is not a commit" in event.text for event in check.store.events())


def test_the_newest_build_is_activated_again_after_a_rollback_or_an_unpin(tmp_path, signer, published, monkeypatch) -> None:
    document = release("0.44.0", 20)
    check = checker(tmp_path, signer, published, monkeypatch)
    older, newest = (tmp_path / "runtimes" / name for name in ("0.44.0-older", "0.44.0-newest"))
    for path, identity in ((older, "0" * 64), (newest, document.identity)):
        (path / "bin").mkdir(parents=True)
        (path / "bin" / "python").write_text("")
        (path / "installation.json").write_text(RuntimeReceipt(build=BuildIdentity.of(document), identity=identity, source="server", installed_at=datetime.now(UTC), packages={}).model_dump_json())
    check.store._update(lambda pointer: pointer.model_copy(update={"active": older, "floor": document.order}))
    published.publish(signer, document)
    assert "installed and active" in check.run()  # found installed, activated, nothing downloaded
    assert check.store.pointer().active == newest.resolve()


def test_an_expired_release_is_refused(tmp_path, signer, published, monkeypatch) -> None:
    published.publish(signer, release("0.44.0", 30, expires_at=datetime(2026, 9, 28, 0, 31, tzinfo=UTC)))
    assert "expired" in checker(tmp_path, signer, published, monkeypatch).run()


def test_releases_come_over_https_or_this_computers_loopback_only() -> None:
    ReleaseSource.server("https://interact.example.com")
    ReleaseSource.server("http://127.0.0.1:8817")
    with pytest.raises(ValueError, match="https"):
        ReleaseSource.server("http://interact.example.com")


def test_upgrade_settings_are_never_synced_from_a_server() -> None:
    assert not {"INTERACT_AUTO_UPGRADE", "INTERACT_UPGRADE_PIN", "INTERACT_UPGRADE_CHECK_SECONDS", "INTERACT_UPGRADE_GITHUB"} & set(PORTABLE_ENV)


def test_a_workflow_file_root_never_reaches_the_installed_runtimes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERACT_RUNTIMES", str(tmp_path / "work" / "runtimes"))
    (tmp_path / "work" / "runtimes" / "0.44.0-x").mkdir(parents=True)
    (tmp_path / "work" / "data").mkdir()
    config = MachineConfig(server_url="https://interact.example.com", workspace_id="00000000-0000-0000-0000-000000000001", machine_id="00000000-0000-0000-0000-000000000002",
                           token="t" * 32, permission_ceiling="read_only", working_directory=tmp_path / "work", file_roots=("runtimes/0.44.0-x", "runtimes", "data"))
    usable, refused = config.usable_file_roots()
    assert usable == ((tmp_path / "work" / "data").resolve(),) and set(refused) == {"runtimes/0.44.0-x", "runtimes"}


def test_a_pin_names_one_build_and_only_that_build_passes_the_floor(tmp_path, signer, published, monkeypatch) -> None:
    check = checker(tmp_path, signer, published, monkeypatch, upgrade_pin="000000a")
    check.store._update(lambda pointer: pointer.model_copy(update={"floor": release("0.44.0", 20).order}))
    published.publish(signer, release("0.44.0", 5, commit="000000b"))  # older, not the pinned build
    assert "holding the pinned build 000000a" in check.run()
    installed = []

    def install(self, source, http, document):
        installed.append(document.commit)
        raise RuntimeError("stopped before installing")

    monkeypatch.setattr(UpgradeCheck, "install", install)
    published.publish(signer, release("0.44.0", 10, commit="000000a"))  # older, but the one the person pinned
    check.run()
    assert installed == ["000000a"]


@pytest.mark.parametrize("broken", ["wheel bytes differ", "wheel missing"])
def test_a_release_file_that_fails_to_download_or_verify_is_recorded_not_raised(tmp_path, signer, published, monkeypatch, broken) -> None:
    check = UpgradeCheck(store=RuntimeStore(root=tmp_path / "runtimes"), keys=signer.keys(), config=settings())
    monkeypatch.setattr(UpgradeCheck, "source", lambda self: published.source)
    document = release("0.44.0", 10)
    published.publish(signer, document)
    if broken == "wheel bytes differ":
        for wheel in document.files:
            (published.root / wheel.file).write_bytes(b"tampered")
    said = check.run()
    assert check.store.events()[-1].kind == "refused" and document.label() in said
    assert check.store.pointer().active is None


def test_a_remembered_plain_http_server_elsewhere_is_refused_as_a_source(tmp_path, signer, monkeypatch) -> None:
    check = UpgradeCheck(store=RuntimeStore(root=tmp_path / "runtimes"), keys=signer.keys(), config=settings())
    monkeypatch.setattr(UpgradeCheck, "server", staticmethod(lambda: "http://interact.example.com"))
    assert "not a release source" in check.run()
    assert check.store.events()[-1].kind == "refused"


def test_a_computer_enrolled_before_logins_were_remembered_upgrades_from_its_machine_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert UpgradeCheck.server() is None
    MachineRunner().save(MachineConfig(server_url="http://127.0.0.1:8817", workspace_id="00000000-0000-0000-0000-000000000001", machine_id="00000000-0000-0000-0000-000000000002",
                                       token="t" * 32, permission_ceiling="read_only", working_directory=tmp_path))
    assert UpgradeCheck.server() == "http://127.0.0.1:8817"
    (tmp_path / "config" / "interact" / "login-server").write_text("https://interact.example.com\n")
    assert UpgradeCheck.server() == "https://interact.example.com"


def test_the_supervisor_contract_v1_is_read_as_written_by_supervisors_already_running() -> None:
    written_by_14aba97 = '{"runtime":"/r/0.43.0-x","mode":"report","state":"/r/live/1-1.state.json"}'
    assert Supervision.model_validate_json(written_by_14aba97) == Supervision(version=1, runtime=Path("/r/0.43.0-x"), mode="report", state=Path("/r/live/1-1.state.json"))


def test_a_worker_that_cannot_read_its_contract_never_becomes_a_supervisor_itself(monkeypatch) -> None:
    monkeypatch.setenv("INTERACT_SUPERVISE", "1")
    monkeypatch.setenv(Supervision.variable, '{"version": 2, "something": "newer"}')
    assert Supervision.current() is None and Supervisor.for_arguments(("mcp",), interactive=False) is None
