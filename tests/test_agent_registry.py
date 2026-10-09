"""The run registry: what is running, who started it, what it cost, what it's doing now.

Two properties matter more than the rest.

**Status is DERIVED, never trusted.** A record says "running"; the process may have been killed.
Reading liveness from the pid (the same check `server_registry` uses) means a killed agent
reports as interrupted instead of spinning forever in the UI — the difference between a supervisor
and a decoration.

**The registry is a FIXED path, not debug_dir-relative.** It is cross-process IPC: the CLI
writes, the extension reads, another shell stops a run. If one process had GALAIUS_DEBUG_DIR set
and another didn't they would look in different places — exactly the metering bug 0ef5fa4 fixed,
reintroduced at the feature level. `server_registry._runtime_dir` is pinned for the same reason.
"""

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from galaius.agents import registry as reg
from galaius.agents.events import TOKEN_FIELDS, AgentEvent
from galaius.pinned_directory import DescriptorDirectory
from galaius.private_files import PRIVATE_FILES
from galaius.processes import process_exited
from tests.support import register_run
from tests.support.private_files import loosen


@pytest.fixture(autouse=True)
def _debug_dir_is_ignored(monkeypatch, tmp_path):
    """Prove the registry ignores GALAIUS_DEBUG_DIR — conftest already isolates HOME and
    UserConfig.PATH, so only that specific redirection has to be set up here."""
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", str(tmp_path / "somewhere-else"))


@pytest.fixture
def unversioned_tmp_path(tmp_path):
    """These two cases require an actually unversioned filesystem, not a nested checkout."""
    if any((parent / marker).exists() for parent in (tmp_path, *tmp_path.parents)
           for marker in (".git", ".hg", ".svn")):
        pytest.skip("unversioned temporary-directory fixture unavailable inside an enclosing repository")
    return tmp_path


@pytest.mark.parametrize("enabled", [True, False])
def test_registry_persists_explicit_mesh_choice(enabled):
    register_run(mesh_enabled=enabled)
    assert reg.get_run("r1").mesh_enabled is enabled


def test_registry_preserves_ranked_selection_and_tool_denials():
    first = reg.LaunchCandidate(provider="fixture-a", model="top", catalog_id="example/top", rank=0)
    second = reg.LaunchCandidate(provider="fixture-b", model="next", catalog_id="example/next", rank=1)
    original = register_run(candidates=(first, second), skipped=(reg.SkippedCandidate(candidate=first, reason="unauthenticated"),),
                       denied_tools=("mcp__galaius__report_issue",))
    restored = reg.get_run("r1")
    assert restored.candidates == original.candidates
    assert restored.skipped == original.skipped
    assert restored.denied_tools == original.denied_tools
    with pytest.raises(ValueError):
        reg.AgentRun.model_validate({**original.model_dump(), "denied_tools": ["--bad"]})


def test_historical_record_does_not_enable_mesh_on_resume():
    original = register_run()
    (reg.agents_dir() / "r1.json").write_text(original.model_dump_json(exclude={"mesh_enabled"}))
    assert reg.get_run("r1").mesh_enabled is False


def test_the_registry_ignores_the_debug_dir_override(tmp_path):
    # It must be findable by a process that never saw GALAIUS_DEBUG_DIR.
    assert "somewhere-else" not in str(reg.agents_dir())
    assert reg.agents_dir() == tmp_path / ".galaius" / "out" / "agents"


@pytest.mark.parametrize(
    ("provider", "model"),
    [("codex", "openai/example-model"), ("fixture/local", "openai/gpt-real")],
)
def test_fixture_identity_cannot_write_without_an_isolated_registry(monkeypatch, provider, model):
    monkeypatch.delenv("GALAIUS_AGENTS_DIR", raising=False)

    with pytest.raises(ValueError, match="isolated GALAIUS_AGENTS_DIR"):
        reg.register(
            run_id="fixture-model-leak",
            pid=None,
            provider=provider,
            name="test harness",
            model=model,
        )

    assert not (reg.agents_dir() / "fixture-model-leak.json").exists()


def test_registry_storage_is_private_despite_a_permissive_umask():
    registry = reg.agents_dir()
    parents = (registry.parents[1], registry.parent, registry)
    for directory in parents:
        directory.mkdir(exist_ok=True)
        loosen(directory)

    existing = (
        registry / "r1.json",
        reg.events_path("r1"),
        reg.messages_path("r1"),
        reg.raw_events_path("r1"),
    )
    for path in existing:
        path.write_text("")
        path.chmod(0o664)

    previous_umask = os.umask(0o002)
    try:
        register_run()
        register_run(run_id="r2")
        reg.append_event("r1", AgentEvent(kind="text", text="private event"))
        assert reg.record_message(from_run="r1", to_run="r2", text="private message")
        with reg.open_raw_events("r2", append=False) as stream:
            stream.write(b"private provider frame\n")
    finally:
        os.umask(previous_umask)

    registry_files = [path for path in registry.iterdir() if path.is_file()]
    assert registry_files
    for path in (*parents, *registry_files):
        PRIVATE_FILES.check(path)
    assert not list(registry.glob("*.new")), "an atomic replacement escaped its write boundary"


@pytest.mark.parametrize("attack", ["directory-component", "append-target"])
def test_registry_symlinks_cannot_redirect_private_bytes(attack: str, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "unchanged.bin"
    external.write_bytes(b"unchanged external bytes")
    original_digest = hashlib.sha256(external.read_bytes()).digest()

    if attack == "directory-component":
        (tmp_path / ".galaius").symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            register_run(run_id="symlinked-directory")
        assert not (outside / "out").exists()
    else:
        register_run(run_id="symlinked-file")
        reg.events_path("symlinked-file").symlink_to(external)
        with pytest.raises(OSError):
            reg.append_event(
                "symlinked-file",
                AgentEvent(kind="prompt", text="private fixture payload"),
            )

    assert hashlib.sha256(external.read_bytes()).digest() == original_digest


@pytest.mark.parametrize("append", [False, True], ids=["truncate", "append"])
def test_registry_refuses_hostile_writes_through_a_leaf_link(
    append: bool, directory_backend, tmp_path,
) -> None:
    register_run(run_id="hostile-write")
    external = tmp_path / "external-sentinel.bin"
    external.write_bytes(b"unchanged external sentinel")
    original_digest = hashlib.sha256(external.read_bytes()).digest()
    reg.raw_events_path("hostile-write").symlink_to(external)

    with pytest.raises(OSError):
        with reg.open_raw_events("hostile-write", append=append) as stream:
            stream.write(b"private registry payload")
    assert hashlib.sha256(external.read_bytes()).digest() == original_digest


def _open_descriptors() -> int | None:
    """How many descriptors this process holds, where the system lists them (/proc); else None."""
    listed = Path("/proc/self/fd")
    return len(os.listdir(listed)) if listed.is_dir() else None


_COUNTS_DESCRIPTORS = pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="descriptors are counted through /proc")


@_COUNTS_DESCRIPTORS
@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleanup-ok", "cleanup-error"])
def test_private_replace_closes_directory_handle_when_replacement_open_fails(
    cleanup_fails: bool, monkeypatch: pytest.MonkeyPatch, directory_backend,
) -> None:
    register_run(run_id="failed-replacement")
    real_open = os.open
    failure = OSError("replacement creation failed")
    descriptors = _open_descriptors()

    def fail_replacement_open(path, flags, *args, **kwargs):
        if str(path).endswith(".new"):
            raise failure
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_replacement_open)
    if cleanup_fails:
        def fail_cleanup(*_args, **_kwargs):
            raise OSError("replacement cleanup failed")
        monkeypatch.setattr(os, "unlink", fail_cleanup)

    with pytest.raises(OSError) as caught:
        reg._replace_private(reg.agents_dir() / "failed-replacement.json", b"payload")
    assert caught.value is failure
    assert _open_descriptors() == descriptors
    assert not list(reg.agents_dir().glob("*.new"))


@_COUNTS_DESCRIPTORS
def test_private_replace_does_not_unlink_an_exclusive_create_collision(
    monkeypatch: pytest.MonkeyPatch, directory_backend,
) -> None:
    register_run(run_id="collision")
    target = reg.agents_dir() / "collision.json"
    original_target = target.read_bytes()
    original_target_mode = stat.S_IMODE(target.stat().st_mode)
    token = "fixedcollision"
    replacement = reg.agents_dir() / f".{target.name}.{token}.new"
    sentinel = b"pre-existing unowned replacement"
    replacement.write_bytes(sentinel)
    replacement.chmod(0o640)
    original_mode = stat.S_IMODE(replacement.stat().st_mode)
    descriptors = _open_descriptors()
    monkeypatch.setattr(reg.secrets, "token_hex", lambda _size: token)

    try:
        with pytest.raises(FileExistsError) as caught:
            reg._replace_private(target, b"new payload")
        assert caught.value.errno == errno.EEXIST
        assert Path(caught.value.filename).name == replacement.name
        assert _open_descriptors() == descriptors
        assert replacement.exists(), "collision cleanup deleted an unowned pre-existing leaf"
        assert target.read_bytes() == original_target
        assert stat.S_IMODE(target.stat().st_mode) == original_target_mode
        assert replacement.read_bytes() == sentinel
        assert stat.S_IMODE(replacement.stat().st_mode) == original_mode
    finally:
        replacement.unlink(missing_ok=True)


@_COUNTS_DESCRIPTORS
def test_private_replace_rejects_candidate_substitution_observed_before_publication(
    monkeypatch: pytest.MonkeyPatch, directory_backend,
) -> None:
    """Detect a substitution already present before publication; this does not claim that the
    later validation-to-rename window is atomic against a malicious same-UID process."""
    register_run(run_id="publication")
    target = reg.agents_dir() / "publication.json"
    original_target = target.read_bytes()
    original_target_mode = stat.S_IMODE(target.stat().st_mode)
    token = "fixedpublication"
    candidate = reg.agents_dir() / f".{target.name}.{token}.new"
    displaced = reg.agents_dir() / f".{target.name}.{token}.owned"
    forged = b"forged unowned publication"
    forged_mode = 0o640
    real_fsync = os.fsync
    owned_descriptor: int | None = None
    owned_identity: tuple[int, int] | None = None
    descriptors = _open_descriptors()

    def substitute_after_fsync(descriptor: int) -> None:
        nonlocal owned_descriptor, owned_identity
        real_fsync(descriptor)
        if owned_descriptor is not None:
            return
        owned_descriptor = descriptor
        opened = os.fstat(descriptor)
        owned_identity = (opened.st_dev, opened.st_ino)
        candidate.rename(displaced)
        attacker = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, forged_mode)
        try:
            assert os.write(attacker, forged) == len(forged)
            os.fchmod(attacker, forged_mode)
        finally:
            os.close(attacker)

    monkeypatch.setattr(reg.secrets, "token_hex", lambda _size: token)
    monkeypatch.setattr(os, "fsync", substitute_after_fsync)

    failure: OSError | None = None
    try:
        try:
            reg._replace_private(target, b"owned payload")
        except OSError as error:
            failure = error
        target_bytes = target.read_bytes()
        target_mode = stat.S_IMODE(target.stat().st_mode)
        candidate_bytes = candidate.read_bytes() if candidate.exists() else None
        candidate_mode = stat.S_IMODE(candidate.stat().st_mode) if candidate.exists() else None
        owned_closed = owned_descriptor is not None
        if owned_descriptor is not None:
            try:
                os.fstat(owned_descriptor)
            except OSError as error:
                owned_closed = error.errno == errno.EBADF
            else:
                owned_closed = False
        directory_closed = _open_descriptors() == descriptors
        actionable = failure is not None and "identity" in str(failure).lower()
        displaced_stat = displaced.stat()
        displaced_identity = (displaced_stat.st_dev, displaced_stat.st_ino)
        assert (
            actionable,
            target_bytes,
            target_mode,
            candidate_bytes,
            candidate_mode,
            owned_closed,
            directory_closed,
            displaced_identity,
        ) == (
            True,
            original_target,
            original_target_mode,
            forged,
            forged_mode,
            True,
            True,
            owned_identity,
        ), "candidate substitution observed before publication must fail without publishing it"
    finally:
        target.write_bytes(original_target)
        target.chmod(original_target_mode)
        candidate.unlink(missing_ok=True)
        displaced.unlink(missing_ok=True)


@pytest.mark.parametrize("failure_point", ["read", "fdopen"])
def test_private_read_preserves_primary_failure_and_closes_descriptor_once(
    failure_point: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_run(run_id="failed-read")
    path = reg.agents_dir() / "failed-read.json"
    original = path.read_bytes()
    original_mode = stat.S_IMODE(path.stat().st_mode)
    descriptor_count = _open_descriptors()
    real_close = os.close
    failure = OSError(errno.EIO, "primary read failure")
    descriptor: int | None = None
    close_attempts = 0

    def failing_fdopen(raw_descriptor: int, *_args, **_kwargs):
        nonlocal close_attempts, descriptor
        descriptor = raw_descriptor
        if failure_point == "fdopen":
            raise failure
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.read.side_effect = failure

        def close_stream(*_args) -> None:
            nonlocal close_attempts
            close_attempts += 1
            real_close(raw_descriptor)

        stream.__exit__.side_effect = close_stream
        return stream

    def close_raw(raw_descriptor: int) -> None:
        nonlocal close_attempts
        if raw_descriptor == descriptor:
            close_attempts += 1
        real_close(raw_descriptor)

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    monkeypatch.setattr(os, "close", close_raw)

    with pytest.raises(OSError) as caught:
        reg._read_private(path)

    assert caught.value is failure
    assert close_attempts == 1
    assert descriptor is not None
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF
    assert _open_descriptors() == descriptor_count
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == original_mode
    assert not list(reg.agents_dir().glob("*.new"))


@pytest.mark.parametrize("reader", ["get_run", "list_runs"])
def test_registry_record_reads_never_follow_a_link_swapped_in_during_open(
    reader: str, directory_backend, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    register_run(run_id="race")
    canonical = reg.agents_dir() / "race.json"
    external = tmp_path / "same-inode-record.json"
    original = canonical.read_bytes()
    os.link(canonical, external)
    real_open = os.open
    swaps = 0

    def race_open(path, flags, *args, **kwargs):
        nonlocal swaps
        if str(path).endswith("race.json") and not swaps:
            swaps += 1
            canonical.unlink()
            canonical.symlink_to(external)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race_open)

    found = reg.get_run("race") if reader == "get_run" else [run for run in reg.list_runs() if run.run_id == "race"]
    assert (swaps, found) == (1, None if reader == "get_run" else [])
    assert external.read_bytes() == original


@pytest.mark.parametrize(
    "seam", ["raw-transcript", "fallback-mirror", "messages", "raw-line-count", "carry-observed-at"],
)
def test_public_registry_reads_never_follow_hostile_private_leaves(
    seam: str, directory_backend, tmp_path,
) -> None:
    register_run(run_id="r1")
    register_run(run_id="r2")
    stored = reg.get_run("r1")
    assert stored is not None
    external = tmp_path / f"external-{seam}.jsonl"
    hostile_text = "forged external transcript"
    if seam == "raw-transcript":
        target = reg.raw_events_path("r1")
        payload = json.dumps({
            "type": "assistant", "session_id": "s",
            "message": {"role": "assistant", "content": [{"type": "text", "text": hostile_text}]},
        }) + "\n"
    elif seam == "fallback-mirror":
        target = reg.events_path("r1")
        payload = AgentEvent(kind="text", text=hostile_text).model_dump_json() + "\n"
    elif seam == "messages":
        target = reg.messages_path("r1")
        payload = AgentEvent(kind="message", text=hostile_text, from_run="r2", to_run="r1").model_dump_json() + "\n"
    elif seam == "raw-line-count":
        target = reg.raw_events_path("r1")
        payload = "hostile\n" * 7
    else:
        reg.raw_events_path("r1").write_text(
            '{"type":"system","subtype":"init","cwd":"/work","tools":[],"session_id":"s"}\n'
        )
        target = reg.events_path("r1")
        payload = AgentEvent(kind="started", text="", at=123.0).model_dump_json() + "\n"
    external.write_text(payload)
    digest = hashlib.sha256(external.read_bytes()).digest()
    target.symlink_to(external)

    if seam == "raw-line-count":
        assert reg.record_message(from_run="r1", to_run="r2", text="safe")
        recorded = [event for event in reg.read_events("r1") if event.kind == "message"]
        assert recorded and recorded[-1].raw_index != 7
    else:
        observed = reg.read_events("r1")
        assert hostile_text not in [event.text for event in observed]
        assert 123.0 not in [event.at for event in observed]
    assert hashlib.sha256(external.read_bytes()).digest() == digest


def test_a_registered_run_is_listed():
    register_run()
    runs = reg.list_runs()
    assert [r.run_id for r in runs] == ["r1"]
    assert runs[0].name == "tester" and runs[0].provider == "claude"


@pytest.mark.parametrize(
    "finish_exit_code, alive, expected_status",
    [
        pytest.param(None, False, "interrupted", id="dead_pid_never_finished"),
        pytest.param(None, True, "running", id="live_pid"),
        pytest.param(0, False, "done", id="finished_exit_0"),
        pytest.param(2, False, "failed", id="finished_exit_nonzero"),
    ],
)
def test_status_is_derived_from_pid_liveness_and_recorded_exit(
    monkeypatch, finish_exit_code, alive, expected_status
):
    """Status is never trusted from the record alone: a live pid overrides a stale 'done', a dead
    pid with no recorded outcome reads as interrupted, and a recorded exit code (clean or not)
    survives the process going away."""
    register_run(pid=999999 if not alive else 1)
    if finish_exit_code is not None:
        reg.finish("r1", exit_code=finish_exit_code)
    monkeypatch.setattr(reg, "_alive", lambda pid: alive)
    run = reg.list_runs()[0]
    assert run.status == expected_status
    if finish_exit_code is not None:
        assert run.exit_code == finish_exit_code


#: The ONE status rule, as a table: what the record says never decides, the process and the run's
#: own stream do. Every surface that reports a run (the CLI, the mirrored machine records, the
#: every listing surface) answers from this rule and nothing else — the owner saw runs sitting at
#: "still working" for hours because each surface carried its own half of it (2026-09-30).
@pytest.mark.parametrize(
    "alive, reused, terminal, expected_status, ended",
    [
        pytest.param(True, False, None, "running", False, id="live_pid"),
        pytest.param(False, False, reg.AgentEvent(kind="done", cost_usd=0.4), "done", True, id="dead_pid_final_event"),
        pytest.param(False, False, reg.AgentEvent(kind="error", text="blew up"), "failed", True, id="dead_pid_error_event"),
        pytest.param(False, False, None, "interrupted", True, id="dead_pid_no_final_event"),
        pytest.param(True, True, None, "interrupted", True, id="pid_reused_by_another_process"),
    ],
)
def test_one_status_rule_answers_every_surface(monkeypatch, alive, reused, terminal, expected_status, ended):
    """A pid its process no longer owns is a DEAD run: the pid alone says "alive" and the record
    would stay "running" forever, which is exactly the stuck row. `pid_started` settles it."""
    register_run(pid=4242)
    reg._update_fields("r1", {"pid_started": 111})
    reg.append_event("r1", reg.AgentEvent(kind="tool", tool="Bash"))
    if terminal is not None:
        reg.append_event("r1", terminal)
    monkeypatch.setattr(reg, "_alive", lambda pid: alive)
    monkeypatch.setattr(reg, "process_started", lambda pid: 222 if reused else 111)
    run = reg.list_runs()[0]
    assert run.status == expected_status
    # An ended run carries WHEN it stopped: the last moment we know it was alive. A row with no
    # end time reads as "1 ms" or "—" wherever the run is listed.
    assert (run.finished_at is not None) is ended
    # And the record itself is fixed on disk, so a surface reading the file sees the same thing.
    assert json.loads((reg.agents_dir() / "r1.json").read_text())["status"] == expected_status


linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


def _child(*argv: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", *argv])


@linux_only
def test_a_zombie_run_is_settled_as_interrupted():
    """A run whose process EXITED but was never collected by its parent still answers every pid probe:
    two finished critic runs read « En cours » for two hours that way (2026-10-06)."""
    child = _child("pass")
    try:
        deadline = time.monotonic() + 10
        while not process_exited(child.pid):
            assert time.monotonic() < deadline, "child never became a zombie"
            time.sleep(0.02)
        register_run(pid=child.pid)
        assert not reg._alive(child.pid)
        (settled,) = reg.settle_gone()
        assert settled.status == "interrupted" and settled.interruption is not None
    finally:
        child.wait()


@linux_only
@pytest.mark.parametrize("reused", [False, True], ids=["killed", "pid_reused"])
def test_settle_gone_writes_interrupted_with_the_cause(reused):
    """`kill -9` with nobody watching, or the pid handed to another program: the minute sweep writes
    `interrupted` + when it was seen gone, on disk, and leaves a live run alone."""
    victim, bystander = _child("import time; time.sleep(60)"), _child("import time; time.sleep(60)")
    try:
        register_run(pid=victim.pid)
        register_run(run_id="alive", pid=bystander.pid)
        if reused:
            reg._update_fields("r1", {"pid_started": -1})
        else:
            victim.kill()
            victim.wait()
        before = time.time()
        assert [run.run_id for run in reg.settle_gone()] == ["r1"]
        stored = json.loads((reg.agents_dir() / "r1.json").read_text())
        assert stored["status"] == "interrupted"
        assert stored["interruption"]["cause"] == "process_gone" and stored["interruption"]["at"] >= before - 1
        assert reg.get_run("alive").status == "running" and reg.settle_gone() == []
        assert "process gone at" in reg.get_run("r1").interruption.describe()
    finally:
        for child in (victim, bystander):
            child.kill()
            child.wait()


#: The user journal's lines for a scope, as `journalctl --user --all -o json USER_UNIT=…` prints them.
_STARTED = {"MESSAGE_ID": "39f53479d3a045ac8e11786248231fbf", "__REALTIME_TIMESTAMP": "1791277759553166"}
_OOMD = {"MESSAGE_ID": "d989611b15e44c9dbf31e3c81256e4ed", "__REALTIME_TIMESTAMP": "1791288056578685"}
_FAILED_OOM = {"MESSAGE_ID": "d9b373ed55a64feb8242e02dbe79a49c", "UNIT_RESULT": "oom-kill", "__REALTIME_TIMESTAMP": "1791288056853530"}
_CONSUMED = {"MESSAGE_ID": "ae8f7b866b0347b9af31fe1c80b127c0", "__REALTIME_TIMESTAMP": "1791288056854595"}
#: The kernel's kill inside a scope that keeps running (`OOMPolicy=continue`): no failed result follows.
_KERNEL_OOM = {"MESSAGE_ID": "fe6faa94e7774663a0da52717891d8ef", "__REALTIME_TIMESTAMP": "1791288056578685"}


@pytest.mark.parametrize("lines, cause, source, at", [
    pytest.param([_STARTED, _OOMD, _FAILED_OOM, _CONSUMED], "out_of_memory", "journal", 1791288056.854595, id="oomd_killed"),
    pytest.param([_STARTED, _KERNEL_OOM, _CONSUMED], "out_of_memory", "journal", 1791288056.854595, id="kernel_killed_in_scope"),
    pytest.param([_STARTED, _CONSUMED], "process_gone", "journal", 1791288056.854595, id="scope_ended"),
    pytest.param([_STARTED], "process_gone", "observed", 5.0, id="no_ending_in_journal"),
    pytest.param([], "process_gone", "observed", 5.0, id="journal_unreadable"),
])
def test_interruption_reads_the_units_journal(monkeypatch, lines, cause, source, at):
    monkeypatch.setattr(reg.Interruption, "_journal", classmethod(lambda cls, unit: lines))
    found = reg.Interruption.observe("run-r37e1.scope", now=5.0)
    assert (found.cause, found.source) == (cause, source) and found.at == pytest.approx(at)


def test_a_child_killed_before_the_runs_last_output_is_not_its_end(monkeypatch):
    """The agent wrote after the kernel took one of its children: something else ended it."""
    monkeypatch.setattr(reg.Interruption, "_journal", classmethod(lambda cls, unit: [_STARTED, _KERNEL_OOM, _CONSUMED]))
    found = reg.Interruption.observe("run-r37e1.scope", now=5.0, since=1791288056.7)
    assert (found.cause, found.source) == ("process_gone", "journal")


def test_a_stale_reaper_cannot_finish_a_reused_run_pid():
    register_run(pid=111)
    first = reg.get_run("r1")
    assert first is not None and first.lifecycle_token
    reg.begin_turn("r1", pid=111)
    current = reg.get_run("r1")
    assert current is not None and current.lifecycle_token != first.lifecycle_token

    assert reg.finish(
        "r1", exit_code=0, expected_pid=111,
        expected_lifecycle_token=first.lifecycle_token,
    ) is False
    current = reg.get_run("r1")
    assert current is not None and current.pid == 111 and current.status == "running"

    assert reg.finish(
        "r1", exit_code=0, expected_pid=111,
        expected_lifecycle_token=current.lifecycle_token,
    ) is True


def test_stop_rejects_a_stale_lifecycle_token():
    register_run(pid=None)
    first = reg.get_run("r1")
    assert first is not None and first.lifecycle_token
    reg.begin_turn("r1", pid=None)
    current = reg.get_run("r1")
    assert current is not None and current.lifecycle_token != first.lifecycle_token

    assert reg.stop("r1", expected_lifecycle_token=first.lifecycle_token) is False
    assert reg.get_run("r1").status == "running"


def test_stop_fails_closed_when_queue_cancellation_cannot_persist(monkeypatch):
    from galaius.agents import agent_queue

    register_run(pid=None)
    monkeypatch.setattr(
        agent_queue, "cancel_pending_locked",
        lambda run_id: (_ for _ in ()).throw(OSError("queue write failed")),
    )
    terminated = []
    monkeypatch.setattr(reg, "_terminate", lambda pid: terminated.append(pid))

    assert reg.stop("r1") is False
    assert terminated == []
    assert reg.get_run("r1").status == "running"


def test_derived_fields_cannot_clobber_a_same_pid_new_turn(monkeypatch):
    register_run(pid=111)
    monkeypatch.setattr(reg, "_alive", lambda pid: True)
    entered = threading.Event()
    release = threading.Event()
    stale_events = [AgentEvent(kind="text", text="stale reply", cost_usd=1.0)]
    real_read_events = reg.read_events

    def delayed_read_events(run_id):
        entered.set()
        assert release.wait(timeout=10)
        return stale_events if run_id == "r1" else real_read_events(run_id)

    monkeypatch.setattr(reg, "read_events", delayed_read_events)
    result = {}

    def derive():
        result["runs"] = reg.list_runs()

    worker = threading.Thread(target=derive)
    worker.start()
    assert entered.wait(timeout=10)
    current = reg.begin_turn("r1", pid=111)
    assert current is not None and current.lifecycle_token
    reg._merge_record_locked("r1", {"last": "new turn", "cost_usd": 2.0})
    release.set()
    worker.join(timeout=10)

    stored = reg.get_run("r1")
    assert not worker.is_alive()
    assert stored is not None and stored.lifecycle_token == current.lifecycle_token
    assert stored.last == "new turn" and stored.cost_usd == 2.0


# ── the spawn tree: who launched whom, across providers ──────────────────────────────────────


def test_a_child_records_its_parent():
    register_run(run_id="parent")
    register_run(run_id="child", parent_run_id="parent", provider="codex")
    tree = {r.run_id: r.parent_run_id for r in reg.list_runs()}
    assert tree == {"parent": None, "child": "parent"}


def test_the_tree_spans_providers():
    # The whole point: a Claude agent starting a Codex agent is an ordinary record, not a
    # special case — they met on MCP.
    register_run(run_id="p", provider="claude")
    register_run(run_id="c", provider="codex", parent_run_id="p")
    kids = reg.children_of("p")
    assert [k.provider for k in kids] == ["codex"]


# ── events: what it is doing now, and what it cost ───────────────────────────────────────────


def test_the_last_event_is_what_it_is_doing_now():
    register_run()
    reg.append_event("r1", reg.AgentEvent(kind="tool", tool="Bash"))
    reg.append_event("r1", reg.AgentEvent(kind="text", text="thinking about it"))
    assert reg.last_event("r1").text == "thinking about it"


def test_cost_totals_come_from_the_events():
    register_run()
    reg.append_event("r1", reg.AgentEvent(kind="text", output_tokens=10))
    reg.append_event("r1", reg.AgentEvent(kind="done", cost_usd=0.25, output_tokens=4))
    run = reg.list_runs()[0]
    assert run.cost_usd == pytest.approx(0.25)


def test_a_run_with_no_events_reports_no_cost_not_zero():
    # Zero would read as "free"; unknown is the truth before anything has been reported.
    register_run()
    assert reg.list_runs()[0].cost_usd is None


def test_events_survive_a_corrupt_line():
    register_run()
    reg.append_event("r1", reg.AgentEvent(kind="text", text="one"))
    (reg.agents_dir() / "r1.jsonl").open("a").write("{ this is not json\n")
    reg.append_event("r1", reg.AgentEvent(kind="text", text="two"))
    assert reg.last_event("r1").text == "two"


# ── seeing agents we did NOT spawn ───────────────────────────────────────────────────────────


def test_foreign_sessions_are_listed_without_duplicating_our_own(monkeypatch):
    """`claude agents --json` sees the user's own interactive windows too — that is what makes
    this a view of the machine. But it also sees the runs WE spawned, so they must not appear
    twice: the vendor's sessionId and our run_id are deliberately the same string."""
    register_run(run_id="43840bfe-mine")
    monkeypatch.setattr(reg, "_alive", lambda pid: True)
    monkeypatch.setattr(
        reg, "_discover_foreign",
        lambda: [
            {"sessionId": "43840bfe-mine", "cwd": "/tmp", "kind": "background", "name": "ours"},
            {"sessionId": "other-1", "cwd": "/home/x", "kind": "interactive", "name": "theirs"},
        ],
    )
    runs = reg.list_runs(include_foreign=True)
    ids = [r.run_id for r in runs]
    assert ids.count("43840bfe-mine") == 1, "our own run was listed twice"
    assert "other-1" in ids
    foreign = next(r for r in runs if r.run_id == "other-1")
    assert foreign.foreign is True and foreign.name == "theirs"


def test_foreign_runs_are_excluded_by_default(monkeypatch):
    monkeypatch.setattr(reg, "_discover_foreign", lambda: [{"sessionId": "x", "name": "n"}])
    assert reg.list_runs() == []


def test_stopping_records_the_outcome(monkeypatch):
    register_run()
    killed = []
    monkeypatch.setattr(reg, "_terminate", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(reg, "_alive", lambda pid: True)
    assert reg.stop("r1") is True
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    assert reg.list_runs()[0].status == "stopped"
    assert killed == [1]


def test_stopping_an_unknown_run_is_false_not_an_exception():
    assert reg.stop("nope") is False


def test_a_corrupt_record_does_not_break_the_listing():
    register_run()
    (reg.agents_dir() / "broken.json").write_text("{ not json")
    assert [r.run_id for r in reg.list_runs()] == ["r1"]


@pytest.mark.parametrize("alias", ["mismatched-bytes", "leaf-symlink"])
def test_registry_readers_reject_a_record_that_does_not_bind_its_filename(alias: str) -> None:
    register_run(run_id="run-b")
    genuine_path = reg.agents_dir() / "run-b.json"
    requested_path = reg.agents_dir() / "run-a.json"
    if alias == "mismatched-bytes":
        requested_path.write_bytes(genuine_path.read_bytes())
    else:
        requested_path.symlink_to(genuine_path.name)

    requested = reg.get_run("run-a")
    genuine = reg.get_run("run-b")
    listed = reg.list_runs()

    assert requested is None
    assert genuine is not None and genuine.run_id == "run-b"
    assert [run.run_id for run in listed] == ["run-b"], (
        "listing must use the same canonical no-follow, filename-bound reader as get_run"
    )


def test_the_record_round_trips_through_json():
    register_run(task="a task with \"quotes\" and ünicode")
    raw = json.loads((reg.agents_dir() / "r1.json").read_text())
    assert raw["task"] == 'a task with "quotes" and ünicode'


def test_the_row_keeps_the_last_MEANINGFUL_line(monkeypatch):
    """A real stream interleaves hook/system events that summarise to nothing. Taking the last
    event literally blanks the supervisor row mid-run, so the row keeps the last line that
    actually says something."""
    register_run()
    reg.append_event("r1", reg.AgentEvent(kind="tool", tool="Bash"))
    reg.append_event("r1", reg.AgentEvent(kind="other", raw_type="system"))
    monkeypatch.setattr(reg, "_alive", lambda pid: True)
    assert reg.list_runs()[0].last == "using Bash"


def test_the_record_on_disk_carries_cost_and_activity(monkeypatch):
    """The VS Code panel reads these files directly and cannot replay a JSONL per row. If cost and
    the activity line lived only on Python's read path, a finished agent would render there as
    'running, —, blank' — which is exactly what it did before this was folded in."""
    register_run()
    reg.append_event("r1", reg.AgentEvent(kind="tool", tool="Bash"))
    reg.append_event("r1", reg.AgentEvent(kind="done", cost_usd=0.61))
    reg.finish("r1", exit_code=0)

    raw = json.loads((reg.agents_dir() / "r1.json").read_text())
    assert raw["cost_usd"] == pytest.approx(0.61)
    assert raw["last"] == "done"
    assert raw["status"] == "done", "a finished run must not still claim to be running on disk"


def test_costs_accumulate_across_events():
    register_run()
    reg.append_event("r1", reg.AgentEvent(kind="done", cost_usd=0.10))
    reg.append_event("r1", reg.AgentEvent(kind="done", cost_usd=0.05))
    assert reg.list_runs()[0].cost_usd == pytest.approx(0.15)


@pytest.mark.parametrize(
    "event, expected_status",
    [
        pytest.param(reg.AgentEvent(kind="done", cost_usd=0.6), "done", id="terminal_done_event"),
        pytest.param(reg.AgentEvent(kind="error", text="blew up"), "failed", id="terminal_error_event"),
        pytest.param(reg.AgentEvent(kind="tool", tool="Bash"), "interrupted", id="no_terminal_event"),
    ],
)
def test_status_is_derived_from_the_last_stream_event_when_the_pid_is_gone(
    monkeypatch, event, expected_status
):
    """If the supervising process dies, no exit code is recorded and the pid is gone — but the
    child's own stream says how it ended (or didn't). The transcript outranks our bookkeeping,
    otherwise a successful run is libelled as crashed and a real crash reads as one too."""
    register_run(pid=999999)
    reg.append_event("r1", event)
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    assert reg.list_runs()[0].status == expected_status


def test_a_healed_status_is_written_back_to_disk(monkeypatch):
    """The panel reads the file, not Python's in-memory view, and only ever downgrades. A status
    healed in memory but left stale on disk shows up there as 'interrupted' regardless."""
    register_run(pid=999999)
    reg.append_event("r1", reg.AgentEvent(kind="done", cost_usd=0.6))
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    reg.list_runs()
    assert json.loads((reg.agents_dir() / "r1.json").read_text())["status"] == "done"


def test_an_ended_run_gets_an_observed_end_time(monkeypatch):
    """A dead process never calls finish(), so 'ended, but no end time' is the COMMON case, not an
    edge one — and a timeline cannot draw an interval without one. The last byte the child wrote
    is an honest observed end: the last moment we know it was alive."""
    register_run(pid=999999)
    reg.raw_events_path("r1").write_text(
        '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.2,'
        '"usage":{},"session_id":"s"}\n'
    )
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    run = reg.list_runs()[0]
    assert run.status == "done"
    assert run.finished_at is not None and run.finished_at >= run.started_at - 1


def test_a_running_run_is_never_given_an_end_time(monkeypatch):
    register_run()
    reg.raw_events_path("r1").write_text("{}\n")
    monkeypatch.setattr(reg, "_alive", lambda pid: True)
    assert reg.list_runs()[0].finished_at is None


def test_the_raw_stream_is_mirrored_into_a_provider_agnostic_file():
    """The panel cannot parse a vendor dialect, and teaching it every provider's JSON would
    duplicate the adapters in TypeScript. Python translates once; the panel reads one shape."""
    register_run()
    reg.raw_events_path("r1").write_text(
        '{"type":"system","subtype":"init","cwd":"/tmp","tools":[],"session_id":"s"}\n'
        '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.3,'
        '"usage":{},"session_id":"s"}\n'
    )
    reg.read_events("r1")
    mirrored = [json.loads(l) for l in (reg.agents_dir() / "r1.jsonl").read_text().splitlines()]
    assert [m["kind"] for m in mirrored] == ["started", "done"]
    assert all("kind" in m and "text" in m for m in mirrored)  # the shape agents.ts expects


_CLAUDE_LINES = [
    '{"type":"system","subtype":"init","cwd":"/w","tools":[],"session_id":"s"}',
    '{"type":"assistant","message":{"id":"m1","content":[{"type":"text","text":"Reading été."}],"usage":{"input_tokens":3,"output_tokens":2}},"session_id":"s"}',
    '{"type":"assistant","message":{"id":"m1","content":[{"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"a.py"}}],"usage":{"input_tokens":3,"output_tokens":2}},"session_id":"s"}',
    '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"print(1)"}]},"session_id":"s"}',
    '{"type":"assistant","message":{"id":"m2","content":[{"type":"text","text":"Done."}],"usage":{"input_tokens":5,"output_tokens":1}},"session_id":"s"}',
    '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.3,"usage":{},"session_id":"s"}',
]


def test_the_live_mirror_built_from_appended_lines_equals_a_whole_rebuild():
    """The one process mirroring every live run reads only what each raw stream appended
    (`StreamMirror`); a whole re-parse per append was 0.8 s on an 8.7 MB stream. Pieces cut inside a
    line and inside a UTF-8 character, a message arriving mid-run: the mirror must read as `read_events`
    writes it, every step."""
    for run_id in ("lead", "inc", "full"):
        register_run(run_id=run_id, name=run_id, provider="claude", task="t")
    stream = ("\n".join(_CLAUDE_LINES) + "\n").encode()
    cuts = [0, 40, stream.index("é".encode()) + 1, stream.index(b"tool_use"), stream.index(b'"type":"user"'), stream.index(b"Done"), len(stream)]
    mirror = reg.StreamMirror("inc")

    def mirrored(run_id):
        # A message's recipient (and the id hashed from it) differs between the two runs by construction.
        return [{key: value for key, value in event.items() if key not in ("at", "to_run") and (key != "event_id" or event["kind"] != "message")}
                for event in _mirrored_events(run_id)]

    for step, (start, end) in enumerate(zip(cuts, cuts[1:])):
        for run_id in ("inc", "full"):
            with reg.raw_events_path(run_id).open("ab") as raw:
                raw.write(stream[start:end])
            if step == 3:
                reg.record_message(from_run="lead", to_run=run_id, text="also check b.py")
        mirror.refresh()
        reg.read_events("full")
        assert mirrored("inc") == mirrored("full"), f"step {step}"
    assert [event["kind"] for event in mirrored("inc")][-1] == "done"
    assert reg.get_run("inc").provider_session_id == reg.get_run("full").provider_session_id == "s"


# ── the observed-at stamp: when galaius first SAW an event, not when the vendor sent it ────────
# The vendor writes no timestamps, so idleness used to be derived from the run's start time — an
# agent working for two minutes was stamped HELD and drawn asleep, the harder it worked the deader
# the building looked. galaius cannot know when the agent acted, but it observes the stream, so
# it stamps the moment it first saw each line — the honest clock for a watched workplace.


def _mirrored_events(run_id: str) -> list[dict]:
    return [json.loads(l) for l in reg.events_path(run_id).read_text().splitlines() if l.strip()]


def test_an_event_is_stamped_when_galaius_first_sees_it(monkeypatch):
    monkeypatch.setattr(reg.time, "time", lambda: 1000.0)
    reg._mirror_normalised("r", [AgentEvent(kind="text", text="one")])
    assert _mirrored_events("r")[0]["at"] == 1000.0


def test_an_event_keeps_the_time_it_was_FIRST_seen(monkeypatch):
    """The mirror is rewritten wholesale from a re-parse on every pass. Stamping the clock of the
    moment would move every event's time forward each time, which is the opposite of a timestamp."""
    monkeypatch.setattr(reg.time, "time", lambda: 1000.0)
    reg._mirror_normalised("r", [AgentEvent(kind="text", text="one")])

    monkeypatch.setattr(reg.time, "time", lambda: 2500.0)
    reg._mirror_normalised("r", [
        AgentEvent(kind="text", text="one"),
        AgentEvent(kind="tool", tool="Read", tool_input="a.py"),
    ])

    out = _mirrored_events("r")
    assert out[0]["at"] == 1000.0, "an event already seen was re-stamped with a later clock"
    assert out[1]["at"] == 2500.0, "a newly observed event takes the clock of the moment"


def test_re_mirroring_an_unchanged_stream_rewrites_nothing(monkeypatch):
    """Carrying the stamps forward is also what keeps a settled run free: if every pass re-stamped,
    the payload would differ every time and the panel would re-read a finished run forever."""
    monkeypatch.setattr(reg.time, "time", lambda: 1000.0)
    events = [AgentEvent(kind="text", text="one")]
    reg._mirror_normalised("r", events)
    before = reg.events_path("r").stat().st_mtime_ns

    monkeypatch.setattr(reg.time, "time", lambda: 9999.0)
    reg._mirror_normalised("r", events)
    assert reg.events_path("r").stat().st_mtime_ns == before


# ── the message ledger: who said what to whom ────────────────────────────────────────────────
# A sequence view (lanes per agent, time down, arrows between them) needs the EXCHANGE recorded,
# not just each side's monologue. So a message is written to BOTH transcripts — the sender's, so
# its own history shows what it asked for, and the recipient's, so the reply has context above it.


def test_a_message_lands_in_both_transcripts():
    register_run(run_id="a", name="lead")
    register_run(run_id="b", name="reviewer")
    reg.record_message(from_run="a", to_run="b", text="please review the diff")

    sent = [e for e in reg.read_events("a") if e.kind == "message"]
    got = [e for e in reg.read_events("b") if e.kind == "message"]
    assert sent and got, "an exchange invisible to one side is not an exchange"
    assert sent[0].to_run == "b" and got[0].from_run == "a"
    assert "review the diff" in got[0].text


def test_messages_are_listed_for_a_sequence_view():
    register_run(run_id="a", name="lead")
    register_run(run_id="b", name="reviewer")
    reg.record_message(from_run="a", to_run="b", text="one")
    reg.record_message(from_run="b", to_run="a", text="two")
    pairs = [(m.from_run, m.to_run, m.text) for m in reg.messages()]
    assert pairs == [("a", "b", "one"), ("b", "a", "two")], "order is the whole point of a sequence"


def test_a_message_to_an_unknown_run_is_refused():
    register_run(run_id="a")
    assert reg.record_message(from_run="a", to_run="ghost", text="hi") is False


def test_a_message_survives_a_run_that_has_a_raw_vendor_stream():
    """The real-world case the tests above miss: once a run has its own vendor transcript, that
    stream is authoritative and the mirror is rewritten from it — so a message appended into the
    mirror would be silently clobbered. Messages live beside the stream, not inside it."""
    register_run(run_id="a", name="lead")
    register_run(run_id="b", name="reviewer")
    reg.raw_events_path("b").write_text(
        '{"type":"system","subtype":"init","cwd":"/tmp","tools":[],"session_id":"s"}\n'
    )
    reg.record_message(from_run="a", to_run="b", text="please review")
    reg.read_events("b")  # forces the mirror rewrite

    kinds = [e.kind for e in reg.read_events("b")]
    assert "message" in kinds, "the message was lost behind the vendor stream"
    assert "started" in kinds, "the vendor's own events must still be there"


# ── which PROJECT a run belongs to ───────────────────────────────────────────────────────────
# Grouping on the working directory's basename splits one repo across several groups the moment
# an agent runs in a subfolder — "src" and "tests" appear as separate projects. The repo root is
# the unit people actually mean by "project".


def test_the_project_is_the_repo_root_not_the_working_subfolder(tmp_path):
    repo = tmp_path / "my-repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert reg.project_for(str(sub)) == "my-repo"


def test_a_directory_outside_any_repo_falls_back_to_its_own_name(unversioned_tmp_path):
    plain = unversioned_tmp_path / "scratch"
    plain.mkdir()
    assert reg.project_for(str(plain)) == "scratch"


def test_a_registered_run_carries_its_project(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    work = repo / "pkg"
    work.mkdir()
    register_run(run_id="r9", cwd=str(work))
    assert reg.list_runs()[0].project == "proj"


def test_an_empty_cwd_has_no_project_rather_than_a_wrong_one():
    assert reg.project_for("") == ""


# ── A project is the REPO, not the nearest folder with a manifest ───────────────────────────
# "Project detection is folder level" — reported, fixed, and STILL folder level: a package
# manifest counted as a project root, so a repo's own sub-package filed as a separate project and
# one repo's agents were split across two groups in the panel.


def test_a_sub_package_belongs_to_its_repo_not_to_itself(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "vscode-extension"
    sub.mkdir()
    (sub / "package.json").write_text("{}")
    assert reg.project_for(str(sub)) == "myrepo"


def test_a_nested_manifest_several_levels_down_still_resolves_to_the_repo(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "services" / "api"
    deep.mkdir(parents=True)
    (deep / "pyproject.toml").write_text("")
    assert reg.project_for(str(deep)) == "myrepo"


def test_a_package_with_no_repo_around_it_is_still_its_own_project(unversioned_tmp_path):
    """Not everything is version controlled — a bare package must not fall back to a home dir."""
    pkg = unversioned_tmp_path / "loose-tool"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text("")
    assert reg.project_for(str(pkg)) == "loose-tool"


def test_the_repo_root_itself_resolves_to_itself(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("")
    assert reg.project_for(str(repo)) == "myrepo"


# ── Forgetting a run ────────────────────────────────────────────────────────────────────────
# Nothing could ever remove a run, so the panel grew forever: every agent ever spawned stayed
# listed with its transcript and raw stream on disk. A list backed by an unbounded collection is
# only usable for as long as you have not used it much.


def _finished(run_id="r1", name="reviewer"):
    run = reg.register(run_id=run_id, name=name, provider="claude", task="t", pid=None)
    reg.finish(run_id, exit_code=0)
    return run


def test_forgetting_a_run_removes_it_and_everything_it_wrote(tmp_path, monkeypatch):
    _finished()
    reg.raw_events_path("r1").write_text("{}\n")
    reg.messages_path("r1").write_text("{}\n")
    assert reg.forget("r1") is True
    assert not any(p.exists() for p in (reg.raw_events_path("r1"), reg.messages_path("r1")))
    assert [r.run_id for r in reg.list_runs()] == []


def test_a_RUNNING_agent_is_never_forgotten(tmp_path, monkeypatch):
    """Removing a live run's record would orphan the process: still working, now invisible."""
    reg.register(run_id="live", name="worker", provider="claude", task="t", pid=os.getpid())
    assert reg.forget("live") is False
    assert [r.run_id for r in reg.list_runs()] == ["live"]


def test_clearing_finished_leaves_the_running_ones_alone(tmp_path, monkeypatch):
    _finished("done1")
    _finished("done2")
    reg.register(run_id="live", name="worker", provider="claude", task="t", pid=os.getpid())
    assert sorted(reg.clear_finished()) == ["done1", "done2"]
    assert [r.run_id for r in reg.list_runs()] == ["live"]


def test_clearing_accepts_the_short_id_the_tool_prints(tmp_path, monkeypatch):
    _finished("abcd1234-0000-0000-0000-000000000000")
    assert reg.forget("abcd1234") is True


# ── What a run IS, not just what it said ────────────────────────────────────────────────────
# "What i want is to be able to view an active running agent, context, system prompt (a file link
# is enough), basically everything" — none of that was recorded. A run knew its name but not WHICH
# definition produced it, so there was nothing to link to, and token use was thrown away.


def test_a_run_remembers_the_definition_it_was_spawned_from(tmp_path, monkeypatch):
    run = reg.register(run_id="r1", name="code-reviewer", provider="claude", task="t",
                       pid=None, agent="code-reviewer")
    assert run.agent == "code-reviewer"
    assert reg.list_runs()[0].agent == "code-reviewer"


def test_a_run_with_no_definition_says_so_rather_than_guessing(tmp_path, monkeypatch):
    assert reg.register(run_id="r1", name="claude", provider="claude", task="t", pid=None).agent is None


def test_the_definition_file_is_where_the_system_prompt_lives(tmp_path, monkeypatch):
    """A file link is all he asked for, so the panel needs the path — resolved by the provider,
    since only it knows where its definitions live."""
    definitions = tmp_path / ".claude" / "agents"
    definitions.mkdir(parents=True)
    (definitions / "code-reviewer.md").write_text("---\nname: code-reviewer\n---\n")
    run = reg.register(run_id="r1", name="code-reviewer", provider="claude", task="t", pid=None,
                       agent="code-reviewer")
    assert run.definition_path == str(definitions / "code-reviewer.md")
    assert reg._read_record("r1").definition_path == str(definitions / "code-reviewer.md")


def test_no_definition_means_no_path_rather_than_a_broken_link(tmp_path, monkeypatch):
    assert reg.register(run_id="r1", name="claude", provider="claude", task="t", pid=None).definition_path is None


def test_token_use_accumulates_so_context_size_is_visible(tmp_path, monkeypatch):
    """"context" — you cannot see how much a running agent has consumed without this."""
    reg.register(run_id="r1", name="a", provider="claude", task="t", pid=None)
    for _ in range(2):
        reg.append_event("r1", AgentEvent(kind="text", text="x", input_tokens=100,
                                          output_tokens=20, cached_input_tokens=60,
                                          cache_write_input_tokens=30))
    folded = reg.get_run("r1")
    healed = reg.list_runs()[0]
    assert (healed.input_tokens, healed.output_tokens) == (200, 40)
    assert (healed.cached_input_tokens, healed.cache_write_input_tokens) == (120, 60)
    assert all(getattr(folded, f) == getattr(healed, f) for f in TOKEN_FIELDS), \
        "the live fold and the self-heal must agree on every token field"


def test_tokens_accumulate_for_a_run_with_a_raw_vendor_stream(tmp_path, monkeypatch):
    """Cost was summed from the parsed stream but tokens were not, so every real run showed its
    price and nothing about how much context it had actually used."""
    reg.register(run_id="r1", name="a", provider="claude", task="t", pid=None)
    raw = reg.raw_events_path("r1")
    raw.parent.mkdir(parents=True, exist_ok=True)
    block_line = json.dumps({
        "type": "assistant", "session_id": "s",
        "message": {"id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 500, "cache_read_input_tokens": 300,
                              "output_tokens": 1}},
    })
    result_line = json.dumps({
        "type": "result", "session_id": "s", "total_cost_usd": 0.5,
        "usage": {"input_tokens": 500, "cache_read_input_tokens": 300, "output_tokens": 40},
        "modelUsage": {"m": {"inputTokens": 500, "cacheReadInputTokens": 300,
                             "cacheCreationInputTokens": 0, "outputTokens": 40}},
    })
    # One message's usage repeats on every content-block line; counted once.
    raw.write_text("\n".join([block_line, block_line, result_line]) + "\n")
    assert reg.finish("r1", exit_code=0)
    stored = reg.get_run("r1")  # complete at termination, before anything lists runs
    assert (stored.input_tokens, stored.cached_input_tokens, stored.output_tokens) == (800, 300, 40)
    assert stored.cost_usd == 0.5


# ── A detached run that finished cleanly is not a crash ─────────────────────────────────────
# `agents spawn` returns immediately, so nobody is left waiting to record the exit code. Status
# was inferred from the pid alone, so every detached run read as "interrupted" the moment it finished
# — measured live: a tester that reported "68 passed" was shown with a crash warning.


@pytest.mark.parametrize(
    "raw_line, expected_status",
    [
        pytest.param(
            {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "s", "stop_reason": "end_turn"},
            "done", id="stream_ended_cleanly",
        ),
        pytest.param(
            {"type": "result", "subtype": "error", "is_error": True, "session_id": "s"},
            "failed", id="stream_ended_in_error",
        ),
        pytest.param(
            {"type": "assistant", "session_id": "s",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "half way"}]}},
            "interrupted", id="stream_stopped_mid_stream",
        ),
    ],
)
def test_status_is_derived_from_how_the_raw_vendor_stream_ended(
    tmp_path, monkeypatch, raw_line, expected_status
):
    """`agents spawn` returns immediately, so nobody is left waiting to record the exit code.
    Status derived from the pid alone reads every detached run as crashed the moment it finishes
    — measured live: a tester that reported "68 passed" was shown with a crash warning. The real
    crash must still read as one: no terminal line anywhere in the stream."""
    reg.register(run_id="r1", name="tester", provider="claude", task="t", pid=999_999)
    raw = reg.raw_events_path("r1")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(raw_line) + "\n")
    assert [r for r in reg.list_runs() if r.run_id == "r1"][0].status == expected_status


# --- Someone else's sessions belong to a project too ------------------------------------------
#
# "i have agents in the 'sheets' folder elsewhere, and i can't change and see how they work."
# Foreign runs — the user's own editor windows — were built straight from the discovery payload
# with a cwd and NO project, while every registered run gets one at registration. So the panel's
# workspace switcher, which groups by project, could never offer the folder those sessions were
# actually working in: they were visible in the flat list and unreachable by workspace.


def test_a_foreign_session_is_filed_under_the_project_it_is_working_in(monkeypatch, tmp_path):
    sheets = tmp_path / "dev" / "xp" / "sheets"
    (sheets / ".git").mkdir(parents=True)

    monkeypatch.setattr(reg, "_discover_foreign", lambda: [
        {"sessionId": "s-1", "name": "sheets-ab", "cwd": str(sheets), "kind": "interactive"},
    ])
    found = [r for r in reg.list_runs(include_foreign=True) if r.run_id == "s-1"]
    assert found, "the foreign session vanished"
    assert found[0].project == "sheets", (
        f"filed under {found[0].project!r} — the workspace switcher groups by project, so an "
        "unfiled session cannot be reached by folder")


def test_a_foreign_session_with_no_cwd_claims_no_project(monkeypatch, tmp_path):
    """An unknown directory must not be guessed into a project: a wrong grouping silently merges
    unrelated work, which is worse than an ungrouped row.

    Asserted against a sibling that DOES have a cwd, in the same call. `project` defaults to "",
    so checking the empty case alone passed identically with the stamping reverted — it proved the
    default, not the behaviour.
    """
    somewhere = tmp_path / "dev" / "thing"
    (somewhere / ".git").mkdir(parents=True)
    monkeypatch.setattr(reg, "_discover_foreign", lambda: [
        {"sessionId": "s-2", "name": "nowhere", "cwd": "", "kind": "interactive"},
        {"sessionId": "s-3", "name": "somewhere", "cwd": str(somewhere), "kind": "interactive"},
    ])
    runs = {r.run_id: r for r in reg.list_runs(include_foreign=True)}
    assert runs["s-3"].project == "thing", "the stamping is not happening at all"
    assert runs["s-2"].project == "", "an unknown directory was guessed into a project"


def test_one_shape_for_a_discovered_session(monkeypatch, tmp_path):
    """The record was built twice — once in `list_runs`, once in the CLI's `agents discovered` —
    with the same nine fields, the same `sid[:8]` fallback and the same millisecond division. Add a
    field to AgentRun and the CLI's JSON silently lacks it, and the panel reading that JSON never
    notices. Both paths go through `AgentRun.from_foreign` now; this pins that they agree.
    """
    where = tmp_path / "dev" / "thing"
    (where / ".git").mkdir(parents=True)
    raw = {"sessionId": "s-9", "name": "win", "cwd": str(where), "kind": "interactive",
           "pid": 77, "startedAt": 1_700_000_000_000}

    built = reg.AgentRun.from_foreign(raw)
    assert built is not None
    assert (built.run_id, built.name, built.project, built.pid) == ("s-9", "win", "thing", 77)
    assert built.foreign is True and built.status == "foreign"
    assert built.started_at == 1_700_000_000.0, "milliseconds must become seconds exactly once"

    monkeypatch.setattr(reg, "_discover_foreign", lambda: [raw])
    listed = [r for r in reg.list_runs(include_foreign=True) if r.run_id == "s-9"]
    assert listed and listed[0].model_dump() == built.model_dump(), (
        "the listing builds a different record than from_foreign does")


def test_a_discovered_session_with_no_id_is_not_a_run(monkeypatch, tmp_path):
    assert reg.AgentRun.from_foreign({"name": "nameless"}) is None


# ───────────── Definition link (formerly test_agent_definition_link.py) ────────────────────────
#
# Alan asked to see, for a running agent, "context, system prompt (a file link is enough)". The
# registry has known where a definition's system prompt lives since the `agent` field was added,
# but the path was only resolvable from Python — while the VS Code panel reads records off disk.
# Recording it ON the run makes it reachable to every reader.


@pytest.fixture
def definition_link_home(tmp_path, monkeypatch):
    """A fake definitions directory, so PROVIDERS['claude'].definition_path can be steered per
    test. `.galaius` is created up front because `CatalogConnection.path()` writes there without
    creating the parent — conftest sets HOME here but does not populate the layout."""
    monkeypatch.setattr(reg, "agents_dir", lambda: tmp_path / "agents")
    (tmp_path / ".galaius").mkdir(exist_ok=True)


def test_a_run_that_IS_an_agent_records_where_its_system_prompt_lives(definition_link_home, monkeypatch, tmp_path):
    definition = tmp_path / "visual-critic.md"
    definition.write_text("# visual critic\n")
    monkeypatch.setattr(
        reg.PROVIDERS["claude"], "definition_path", lambda agent: definition if agent == "visual-critic" else None
    )

    run = reg.register(
        run_id="rDL1", pid=1, provider="claude", name="visual-critic", agent="visual-critic"
    )

    assert run.definition_path == str(definition)
    assert reg._read_record("rDL1").definition_path == str(definition), "and it survives the round trip"


def test_a_plain_run_records_no_definition(definition_link_home):
    run = reg.register(run_id="rDL2", pid=2, provider="claude", name="a task")
    assert run.definition_path is None


def test_an_agent_whose_definition_file_is_missing_records_nothing(definition_link_home, monkeypatch):
    monkeypatch.setattr(reg.PROVIDERS["claude"], "definition_path", lambda agent: None)
    run = reg.register(run_id="rDL3", pid=3, provider="claude", name="x", agent="ghost")
    assert run.definition_path is None, "a link to a file that does not exist is worse than none"


def test_a_record_written_before_the_field_existed_is_backfilled(definition_link_home, monkeypatch, tmp_path):
    """Otherwise the client has to keep its own copy of where a provider stores definitions —
    which is the vendor hard-coding this change exists to remove. Records already on disk are
    repaired on read instead, so the guess has nobody left to serve."""
    definition = tmp_path / "researcher.md"
    definition.write_text("# researcher\n")
    monkeypatch.setattr(reg.PROVIDERS["claude"], "definition_path", lambda agent: definition)

    run = reg.register(run_id="rDL4-old", pid=1, provider="claude", name="researcher", agent="researcher")
    stored = reg._record_path("rDL4-old")
    stored.write_text(run.model_dump_json(exclude={"definition_path"}))

    assert reg._read_record("rDL4-old").definition_path == str(definition)
    assert "definition_path" not in stored.read_text(), "reading history does not rewrite its provenance"


def test_server_mode_preserves_retired_history_without_catalog_lookup(definition_link_home, monkeypatch, tmp_path):
    reg.CatalogConnection.path().write_text('{}')
    def unexpected_lookup(agent):
        raise AssertionError('history must not resolve roles against a live catalog')
    monkeypatch.setattr(reg.PROVIDERS['claude'], 'definition_path', unexpected_lookup)
    run = reg.AgentRun(run_id='old-retired', provider='claude', name='retired', agent='retired')
    directory = reg.agents_dir()
    directory.mkdir()
    (directory / 'old-retired.json').write_text(run.model_dump_json(exclude={'definition_path'}))
    (directory / 'old-retired.json').chmod(0o600)
    assert reg._read_record('old-retired').definition_path is None
    assert reg.resolve_run_id('old-retired') == 'old-retired'


def test_backfill_does_not_touch_a_plain_run(definition_link_home, monkeypatch, tmp_path):
    monkeypatch.setattr(reg.PROVIDERS["claude"], "definition_path", lambda agent: tmp_path / "x.md")
    reg.register(run_id="rDL-plain", pid=1, provider="claude", name="a task")
    before = reg._record_path("rDL-plain").read_text()
    assert reg._read_record("rDL-plain").definition_path is None
    assert reg._record_path("rDL-plain").read_text() == before, "no pointless rewrite"


# ── A LATE quota refusal (after the post-spawn probe window) must still cool the model down ──
# `run_agent`'s `_quota_probe` only watches the first few seconds after spawn — a refusal minutes
# into a working run arrives only in the child's own event stream, read through `read_events`'s
# REPARSE path (`mirror_while_alive` polls it repeatedly). `append_event`'s built-in quota catch
# never fires there — nothing calls `append_event` for an ordinary process-kind run, live or
# resumed; it is reachable only from the interactive local_session `_ConversationProjector`. Three
# real runs died on the same refusal in one machine-restart-adjacent morning and `quota-cooldowns.json`
# never got a fresh entry — the next dispatch picked the same cooled model and died again.


def _claude_error_stream(text: str) -> str:
    """The real shape a Claude `-p --output-format stream-json` refusal ends on: an assistant
    turn, then a `result` line with `is_error: true` — exactly what `ClaudeCodeProvider.parse`
    turns into `AgentEvent(kind="error", text=...)`, matching the captured evidence
    (`~/.galaius/out/agents/4d1afafb*.jsonl` et al.)."""
    return (
        json.dumps({"type": "assistant", "session_id": "SID", "message": {
            "content": [{"type": "text", "text": "working on it"}], "usage": {}}}) + "\n"
        + json.dumps({"type": "result", "is_error": True, "session_id": "SID", "result": text}) + "\n"
    )


def test_a_late_refusal_ending_a_fresh_run_records_a_cooldown():
    from galaius.agents import quota

    reg.register(run_id="late1", pid=222, provider="claude", name="w",
                agent="web-researcher", model="claude-fable-5-1")
    reg.raw_events_path("late1").parent.mkdir(parents=True, exist_ok=True)
    reg.raw_events_path("late1").write_text(_claude_error_stream(
        "You've reached your Fable limit. Switch to another model, or manage usage credits."
    ))
    assert quota.blocked_until("claude", "claude-fable-5-1") is None, "sanity: nothing recorded yet"

    token = reg.get_run("late1").lifecycle_token
    assert reg.finish("late1", exit_code=1, expected_pid=222, expected_lifecycle_token=token) is True

    assert quota.blocked_until("claude", "claude-fable-5-1") is not None, \
        "the late refusal in this run's own event stream never cooled its model down"


def test_a_late_refusal_ending_a_RESUMED_run_records_a_cooldown():
    """A resumed turn re-registers a fresh pid/lifecycle via `begin_turn` (what
    `launch_continuation` does before spawning) and appends to the SAME raw stream — the shape
    `agents send` produces. The refusal arrives on this second turn, after the first turn's own
    successful reply, exactly like the captured evidence's resumed run
    (`e3655140*` — a queued `agents send` delivery that died on the same Fable refusal)."""
    from galaius.agents import quota

    reg.register(run_id="late2", pid=111, provider="claude", name="w",
                agent="app-engineer", model="claude-fable-5-1")
    reg.raw_events_path("late2").parent.mkdir(parents=True, exist_ok=True)
    reg.raw_events_path("late2").write_text(json.dumps({
        "type": "result", "is_error": False, "session_id": "SID", "result": "first turn ok",
    }) + "\n")
    reg.finish("late2", exit_code=0, expected_pid=111,
              expected_lifecycle_token=reg.get_run("late2").lifecycle_token)
    assert quota.blocked_until("claude", "claude-fable-5-1") is None, "a clean turn must not cool anything"

    resumed = reg.begin_turn("late2", pid=333, model="claude-fable-5-1")
    assert resumed is not None and resumed.lifecycle_token
    with reg.raw_events_path("late2").open("a") as f:
        f.write(_claude_error_stream(
            "You've reached your Fable limit. Switch to another model, or manage usage credits."
        ))

    assert reg.finish(
        "late2", exit_code=1, expected_pid=333, expected_lifecycle_token=resumed.lifecycle_token,
    ) is True

    assert quota.blocked_until("claude", "claude-fable-5-1") is not None, \
        "a resumed run's late refusal never cooled its model down"


def test_a_late_refusal_on_an_ORPHANED_run_records_a_cooldown_exactly_once():
    """`agents spawn` deliberately detaches — "the child leads its own session ... so it outlives
    this process" (`run.py::run_agent` docstring) — so its `_reap()` asyncio task dies WITH the
    spawning CLI process, and `finish()` never runs at all: exit_code stays `None` forever. The
    ONLY place such an orphaned run's ending is ever noticed is the LAZY self-heal in `_derive`
    (triggered by any later `list_runs()` — a panel refresh, `agents list`). Two of the three real
    runs that died on the same refusal had exactly this shape (`exit_code: None` in their `.json`).
    Read repeatedly (as a panel polls), it must record the cooldown ONCE, never re-extend it
    forever."""
    from galaius.agents import quota

    reg.register(run_id="orphan1", pid=999999, provider="claude", name="w",
                agent="visual-critic", model="claude-fable-5-1")
    reg.raw_events_path("orphan1").parent.mkdir(parents=True, exist_ok=True)
    reg.raw_events_path("orphan1").write_text(_claude_error_stream(
        "You've reached your Fable limit. Switch to another model, or manage usage credits."
    ))
    # No `reg.finish(...)` call at all — the orphaned shape: exit_code stays None, nothing ever
    # reaped this run's real process end.
    assert quota.blocked_until("claude", "claude-fable-5-1") is None

    first_pass = reg.list_runs()
    found = next(r for r in first_pass if r.run_id == "orphan1")
    assert found.status == "failed" and found.exit_code is None
    first_deadline = quota.blocked_until("claude", "claude-fable-5-1")
    assert first_deadline is not None, "the self-healed orphaned run never cooled its model down"

    # A second, later read (the panel polling again) must not keep pushing the cooldown forward.
    reg.list_runs()
    reg.list_runs()
    assert quota.blocked_until("claude", "claude-fable-5-1") == first_deadline


def test_a_clean_finish_records_no_cooldown():
    from galaius.agents import quota

    reg.register(run_id="late3", pid=444, provider="claude", name="w",
                agent="tester", model="claude-fable-5-1")
    reg.raw_events_path("late3").parent.mkdir(parents=True, exist_ok=True)
    reg.raw_events_path("late3").write_text(json.dumps({
        "type": "result", "is_error": False, "session_id": "SID", "result": "all done",
    }) + "\n")
    reg.finish("late3", exit_code=0, expected_pid=444,
              expected_lifecycle_token=reg.get_run("late3").lifecycle_token)
    assert quota.blocked_until("claude", "claude-fable-5-1") is None


def test_polling_live_runs_rereads_only_records_written_since(monkeypatch):
    """The MCP server polls every second over the whole store; a settled record is read once."""
    import os
    from tests.support import register_run

    register_run("settled", name="s", provider="claude", task="t", pid=None)
    register_run("live", name="l", provider="claude", task="t", pid=os.getpid())
    assert [r.run_id for r in reg.running_runs()] == ["live"]

    reads: list[str] = []
    real = reg._read_record
    monkeypatch.setattr(reg, "_read_record", lambda run_id: reads.append(run_id) or real(run_id))
    assert [r.run_id for r in reg.running_runs()] == ["live"]
    assert reads == []

    reg.finish("live", exit_code=0)
    reads.clear()
    assert reg.running_runs() == []
    assert reads == ["live"]


def test_a_pid_given_to_another_process_is_neither_the_run_nor_stopped_with_it():
    """A run's pid names its process only with the start time recorded beside it: once the system
    gives that pid to another program (Windows reuses pids within minutes), the run reads crashed
    and stopping it leaves that program alone. The run's own process is still stopped, whole tree."""
    from galaius.processes import process_group_options

    def sleeper() -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **process_group_options())

    stranger, own = sleeper(), sleeper()
    try:
        register_run("reused", pid=stranger.pid)
        record = reg.get_run("reused")
        record.pid_started += 1  # the recorded process started earlier than the one holding the pid now
        reg.save_run(record)
        register_run("own", pid=own.pid)

        assert {run.run_id: run.status for run in reg.list_runs()} == {"reused": "interrupted", "own": "running"}
        assert reg.stop("reused") and reg.stop("own")
        assert own.wait(timeout=10) is not None
        assert stranger.poll() is None
    finally:
        for process in (stranger, own):
            process.kill()
            process.wait(timeout=10)


# ── a listing reads a run's transcript only when it changed ────────────────────────────────────


def _said(text: str) -> str:
    return json.dumps({"type": "assistant", "session_id": "s",
                       "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n"


_ENDED = ('{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.25,'
          '"usage":{"input_tokens":40,"output_tokens":7},"session_id":"s"}\n')


@pytest.mark.parametrize(("ended", "change", "rereads"), [
    (True, "nothing", False),
    (False, "process_died", False),
    (False, "stream_grew", True),
    (True, "message_arrived", True),
])
def test_a_listing_rereads_a_transcript_only_when_it_changed(monkeypatch, ended, change, rereads):
    """Re-parsing every transcript on each `agents list` cost ~1 min of CPU at a few thousand runs.
    A listing reads a record's stream digest instead, and must answer exactly what a re-parse of
    the whole stream answers."""
    alive = {"value": True}
    monkeypatch.setattr(reg, "_alive", lambda pid: alive["value"])
    register_run(pid=999999)
    register_run("r0", pid=None)
    reg.raw_events_path("r1").write_text(_said("working") + (_ENDED if ended else ""))
    reg.list_runs()

    if change == "process_died":
        alive["value"] = False
    elif change == "stream_grew":
        with reg.raw_events_path("r1").open("a") as stream:
            stream.write(_said("still working") + _ENDED)
    elif change == "message_arrived":
        assert reg.record_message(from_run="r0", to_run="r1", text="one more thing")
    reads: list[str] = []
    real = reg.read_events
    monkeypatch.setattr(reg, "read_events", lambda run_id: reads.append(run_id) or real(run_id))
    listed = next(run for run in reg.list_runs() if run.run_id == "r1")
    assert ("r1" in reads) is rereads

    record = json.loads(reg._record_path("r1").read_text())
    record.pop("stream_digest")
    reg._record_path("r1").write_text(json.dumps(record))
    fresh = next(run for run in reg.list_runs() if run.run_id == "r1")
    assert listed.model_dump(exclude={"stream_digest"}) == fresh.model_dump(exclude={"stream_digest"})
    # A message after the ending means the run is addressed again: its stream no longer ends there.
    assert listed.status == {"nothing": "done", "process_died": "interrupted",
                             "stream_grew": "done", "message_arrived": "running"}[change]


def test_a_listing_checks_the_registry_folder_once_not_per_record(monkeypatch, directory_backend):
    """Re-checking the private folder chain before every record read was ~3 s of a warm listing
    at three thousand records; a folder pinned by descriptor is checked once per listing, each leaf
    still opened without following a link. A folder reached by path (Windows) is re-checked per
    file: holding one check would leave a folder swapped mid-listing unseen."""
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    checks: list[int] = []
    real = reg._ensure_registry_directory

    def counted() -> Path:
        checks.append(1)
        return real()

    def warm_listing_checks(count: int) -> int:
        for index in range(count):
            register_run(f"r{index}", pid=None)
        reg.list_runs()  # settles and digests every record
        checks.clear()
        monkeypatch.setattr(reg, "_ensure_registry_directory", counted)
        assert len(reg.list_runs()) == count
        monkeypatch.setattr(reg, "_ensure_registry_directory", real)
        return len(checks)

    few, many = warm_listing_checks(3), warm_listing_checks(9)
    assert few == many if directory_backend is DescriptorDirectory else few < many


@pytest.mark.parametrize("separator", [" ", " ", "\x85"])
def test_a_line_separator_inside_an_event_keeps_the_event_whole(separator):
    """JSON leaves these separators unescaped inside strings; only a newline ends a stream line."""
    register_run(run_id="r1")
    said = [f"first{separator}half", "second"]
    reg.raw_events_path("r1").write_text("".join(json.dumps({
        "type": "assistant", "session_id": "s",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }, ensure_ascii=False) + "\n" for text in said), encoding="utf-8")

    texts = [(event.text, event.raw_index) for event in reg.read_events("r1") if event.kind == "text"]

    assert texts == [(said[0], 0), (said[1], 1)]
    assert reg.raw_line_count("r1") == 2


# A run a provider usage limit stopped resumes by itself at the reset the provider named (owner
# 2026-10-09: five builder runs stayed « failed » after their « resets 5:20pm » had passed).
_SESSION_LIMIT = "You've hit your session limit · resets 5:20pm (Europe/Paris)"


def _ended_on(run_id: str, said: str, *, pid: int, parent: str | None = None) -> reg.AgentRun:
    reg.register(run_id=run_id, pid=pid, provider="claude", name="w", agent="app-engineer",
                 model="claude-opus-5-5", parent_run_id=parent)
    reg.raw_events_path(run_id).write_text(_claude_error_stream(said))
    assert reg.finish(run_id, exit_code=1, expected_pid=pid, expected_lifecycle_token=reg.get_run(run_id).lifecycle_token)
    return reg.get_run(run_id)


@pytest.mark.parametrize("said, paused", [
    (_SESSION_LIMIT, True),
    ("You've reached your Fable limit. Switch to another model, or manage usage credits.", False),  # names no reset
    ("Error: the build failed", False),
])
def test_only_a_usage_limit_naming_its_reset_pauses_a_run(said, paused):
    from galaius.agents import quota

    run = _ended_on("lim1", said, pid=401)
    written = reg.schedule_limit_resumes()
    if not paused:
        assert written == [] and reg.get_run("lim1").resume_at is None
        return
    reopens = quota.Refusal.read(said, now=run.finished_at).reopens_at
    assert [r.run_id for r in written] == ["lim1"]
    assert reg.get_run("lim1").resume_at == reopens + quota.RESET_SLACK
    assert 0 < reopens - run.finished_at <= 24 * 3600


def test_a_paused_run_is_claimed_once_at_its_reset_and_its_sub_run_is_left_to_it():
    _ended_on("lim-root", _SESSION_LIMIT, pid=402)
    _ended_on("lim-sub", _SESSION_LIMIT, pid=403, parent="lim-root")
    assert [r.run_id for r in reg.schedule_limit_resumes()] == ["lim-root"]
    at = reg.get_run("lim-root").resume_at

    assert reg.claim_limit_resume("lim-root", now=at - 1) is None, "claimed before its reset"
    assert reg.claim_limit_resume("lim-root", now=at + reg.RESUME_GRACE + 1) is None, "claimed long after the web said it ended"
    claimed = reg.claim_limit_resume("lim-root", now=at + 5)
    assert claimed is not None and claimed.resume_at is None and claimed.resumed_at == at + 5
    assert reg.claim_limit_resume("lim-root", now=at + 6) is None, "a second runner resumed it again"
    assert reg.get_run("lim-sub").resume_at is None


def test_a_resumed_turn_refused_again_right_away_is_left_to_reprendre(monkeypatch):
    """The resumed turn hears the same « resets 5:20pm » just after 17:20: read then, it names TOMORROW's
    17:20 (code-reviewer r1) — the window did not reopen, so it is never paused for a day, nor looped."""
    _ended_on("lim-again", _SESSION_LIMIT, pid=404)
    at = reg.schedule_limit_resumes()[0].resume_at
    claimed = reg.claim_limit_resume("lim-again", now=at)
    resumed = reg.begin_turn("lim-again", pid=405, model="claude-opus-5-5")
    with reg.raw_events_path("lim-again").open("a") as stream:
        stream.write(_claude_error_stream(_SESSION_LIMIT))
    monkeypatch.setattr(reg.time, "time", lambda: claimed.resumed_at + 10)
    assert reg.finish("lim-again", exit_code=1, expected_pid=405, expected_lifecycle_token=resumed.lifecycle_token)
    assert reg.schedule_limit_resumes() == [] and reg.get_run("lim-again").resume_at is None
