"""A run whose folder was removed (a slice worktree removed after its merge) resumes in that same
folder, put back, through the real dispatcher and a real child process; one that cannot be put back
fails with a sentence, never a bare errno."""

import json
import subprocess
import sys

import pytest

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
import threading

from galaius.agents.providers import PROJECT_SETTINGS_OFF, UNTRUSTED_FOLDERS
from galaius.agents.run_folder import MISSING_PREFIX, RESTORED_PREFIX
from tests.support.agents import ScriptedProvider, install_provider, use_policy

REPLY = (
    "import json, os; "
    "print(json.dumps({'type':'assistant','session_id':'vendor','message':{'content':[{'type':'text','text':os.getcwd()}]}})); "
    "print(json.dumps({'type':'result','is_error':False,'session_id':'vendor'}))"
)


class _Provider(ScriptedProvider):
    name = "folder-fake"
    can_resume = True

    def __init__(self, transcript):
        self.transcript = transcript

    def resume_command(self, session_id, message, **kwargs):
        return [sys.executable, "-c", REPLY]

    def session_files(self, home, cwd, session):
        return (self.transcript,)

    def transcript_branch(self, raw):
        return str(raw.get("gitBranch") or "")

    def valid_definition(self, agent):
        return True

    def permission_modes(self):
        return []


def _git(folder, *arguments):
    return subprocess.run(("git", "-C", str(folder), *arguments), check=True, capture_output=True, text=True).stdout.strip()


def _repository(folder):
    folder.mkdir(parents=True)
    _git(folder, "init", "-q", "-b", "main")
    (folder / "sub").mkdir()
    (folder / "sub" / "kept.txt").write_text("kept")
    _git(folder, "add", ".")
    _git(folder, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return folder


def _resume(monkeypatch, tmp_path, cwd, *, environment=None, branch="fix/slice", runs=("r1",)):
    """Resume `runs` in `cwd` at once, as « Continuer » does; their saved conversation says they worked on `branch`."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"cwd": str(cwd), "gitBranch": branch}) + "\n")
    provider = _Provider(transcript)
    install_provider(monkeypatch, provider)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    use_policy(monkeypatch, messaging, agents={"tester": "folder-model"}, providers={provider.name: True})
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    for name, value in (environment or {}).items():
        monkeypatch.setenv(name, value)
    for run_id in runs:
        reg.register(run_id=run_id, pid=999999, provider=provider.name, name="worker", task="t", agent="tester",
                     provider_session_id="vendor", cwd=str(cwd))
        assert messaging.deliver_message(run_id, reg.RESUME_BRIEF, sender="operator").state == "queued"
    dispatchers = [threading.Thread(target=agent_queue.dispatch, args=(run_id,)) for run_id in runs]
    for dispatcher in dispatchers:
        dispatcher.start()
    for dispatcher in dispatchers:
        dispatcher.join()
    return reg.get_run(runs[0])


@pytest.mark.parametrize(("layout", "how"), [
    ("branch_kept", "worktree de proj sur la branche fix/slice"),                    # its branch still exists: back on it
    ("branch_deleted", "worktree de proj au commit"),                                # merged and deleted: the repository's line, detached
    ("branch_in_other_repo", "worktree de proj au commit"),                          # the name says proj: never the other repo holding the branch
    ("subfolder", "worktree de proj sur la branche fix/slice, dans"),                # the run worked in <worktree>/sub: the worktree comes back
    ("tracked", "fichiers suivis de proj"),                                       # a folder its repository holds, removed from the checkout
    ("unnamed_two_holders", "dossier vide"),                                         # no name, two repositories hold the branch: none is guessed
    ("loose", "dossier vide"),                                                       # a folder no repository knows
    ("web_clone", "dossier vide"),                                                   # cloned from the web: no git runs there
    ("web_clone_sibling", "dossier vide"),                                           # the repository it is named after came from the web
    ("ignored_build_output", "dossier vide"),                                        # an ignored folder of a checkout is no worktree
])
def test_a_run_whose_folder_was_removed_resumes_in_it_put_back(monkeypatch, tmp_path, layout, how):
    repository = _repository(tmp_path / "proj")
    other = _repository(tmp_path / "proj-tools")
    slices = tmp_path / ".worktrees"
    folder = {"subfolder": slices / "proj-slice" / "sub", "tracked": repository / "sub", "loose": tmp_path / "loose" / "work",
              "unnamed_two_holders": slices / "elsewhere", "ignored_build_output": repository / "build" / "out"}.get(layout, slices / "proj-slice")
    top = slices / ("elsewhere" if layout == "unnamed_two_holders" else "proj-slice")
    if layout in {"tracked", "loose", "ignored_build_output"}:
        (repository / ".git" / "info" / "exclude").write_text("build/\n")
        subprocess.run(("rm", "-rf", str(repository / "build" if layout == "ignored_build_output" else folder)), check=True)
    else:
        _git(repository, "worktree", "add", "-q", "-b", "fix/slice", str(top))
        _git(repository, "worktree", "add", "-q", "-b", "other", str(slices / "proj-other"))
        _git(other, "worktree", "add", "-q", "-b", "tools", str(slices / "proj-tools-x"))  # both repositories have slices here
        _git(repository, "worktree", "remove", str(top))
        if layout in {"branch_deleted", "branch_in_other_repo"}:
            _git(repository, "branch", "-D", "fix/slice")
        if layout in {"branch_in_other_repo", "unnamed_two_holders"}:
            _git(other, "branch", "fix/slice")
    environment = {"web_clone": {PROJECT_SETTINGS_OFF: "1"}, "web_clone_sibling": {UNTRUSTED_FOLDERS: str(repository)}}.get(layout)

    run = _resume(monkeypatch, tmp_path, folder, environment=environment, branch="main" if layout == "ignored_build_output" else "fix/slice")

    assert [item.state for item in agent_queue.items("r1")] == ["replied"], run.last
    assert folder.is_dir()
    notice = next(event for event in reg.read_events("r1") if event.from_run == reg.FOLDER_RESTORED_SENDER)
    assert notice.text.startswith(f"{RESTORED_PREFIX}{folder} ({how}"), notice.text
    if layout in {"branch_deleted", "branch_in_other_repo"}:
        assert _git(folder, "rev-parse", "HEAD") == _git(repository, "rev-parse", "main")
        assert "la branche fix/slice n’existe plus" in notice.text
    if layout in {"subfolder", "tracked"}:
        assert (folder / "kept.txt").read_text() == "kept"
    if how == "dossier vide":
        assert not any(folder.iterdir())
    # The resumed child ran IN the folder put back (its vendor session is keyed by that path).
    assert any(event.text == str(folder) for event in reg.read_events("r1") if event.kind == "text")
    assert not (reg.agents_dir() / f"{reg.FOLDER_RESTORED_SENDER}.messages.jsonl").exists()


@pytest.mark.parametrize("layout", ["file_in_the_way", "removal_staged"])
def test_a_folder_that_cannot_be_put_back_fails_the_resume_with_why_and_what_to_do(monkeypatch, tmp_path, layout):
    if layout == "file_in_the_way":
        (tmp_path / "blocker").write_text("a file where the folder's parent was")
        folder = tmp_path / "blocker" / "work"
    else:
        repository = _repository(tmp_path / "proj")
        _git(repository, "rm", "-rq", "sub")
        folder = repository / "sub"

    run = _resume(monkeypatch, tmp_path, folder)

    assert run.status == "failed"
    assert run.last.startswith(f"resume refused: {MISSING_PREFIX}{folder}. "), run.last
    assert "« Continuer »" in run.last and "Errno" not in run.last
    assert not folder.is_dir()


def test_runs_resuming_in_one_removed_folder_at_once_put_it_back_once(monkeypatch, tmp_path):
    repository = _repository(tmp_path / "proj")
    folder = tmp_path / ".worktrees" / "proj-slice"
    _git(repository, "worktree", "add", "-q", "-b", "fix/slice", str(folder))
    _git(repository, "worktree", "add", "-q", "-b", "other", str(tmp_path / ".worktrees" / "proj-other"))
    _git(repository, "worktree", "remove", str(folder))

    _resume(monkeypatch, tmp_path, folder, runs=("lead", "specialist"))

    assert [item.state for run_id in ("lead", "specialist") for item in agent_queue.items(run_id)] == ["replied", "replied"]
    assert _git(folder, "branch", "--show-current") == "fix/slice"
    notices = [event for run_id in ("lead", "specialist") for event in reg.read_events(run_id) if event.from_run == reg.FOLDER_RESTORED_SENDER]
    assert len(notices) == 1
