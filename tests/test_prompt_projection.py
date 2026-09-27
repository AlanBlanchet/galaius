import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from interact.prompt_projection import (
    MANIFEST_NAME,
    compile_prompt_projection,
    install_prompt_projection,
    stage_projection_install,
)
from tests.support import commit_all, init_repo, run_git


LEGACY = Path.home() / "dev" / "my-prompts"
LEGACY_COMMIT = "f9b6cca2f01172beac30876c4d39fd479ca9fb4b"


def _git(repository: Path, *arguments: str) -> str:
    return run_git(repository, *arguments).stdout.strip()


def _extract(repository: Path, commit: str, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    records = _git(repository, "ls-tree", "-rz", "--full-tree", commit).encode().split(b"\0")
    for record in filter(None, records):
        metadata, encoded_path = record.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        assert kind == "blob" and mode in {"100644", "100755"}
        path = destination / encoded_path.decode()
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(subprocess.run(
            ["git", "-C", str(repository), "show", blob], capture_output=True, check=True
        ).stdout)
        path.chmod(int(mode[-3:], 8))


def _outputs(root: Path) -> dict[str, tuple[int, int, bytes]]:
    paths = [root / name for name in ("AGENTS.md", "instructions.md", "org.json")]
    paths += sorted((root / "agents").glob("*.md"))
    paths += sorted((root / "skills").glob("*/SKILL.md"))
    paths += sorted((root / "rules").glob("*.md"))
    paths += sorted((root / "hooks").glob("*"))
    return {
        path.relative_to(root).as_posix(): (
            stat.S_IMODE(path.stat().st_mode), path.stat().st_size, path.read_bytes()
        )
        for path in paths if path.is_file()
    }


def test_exact_commit_compiler_matches_the_complete_legacy_generator(tmp_path: Path) -> None:
    if not (LEGACY / ".git").exists():
        pytest.skip("migration source checkout is unavailable")
    # The fixture is one immutable commit; the preserved source may have newer work.
    _git(LEGACY, "cat-file", "-e", f"{LEGACY_COMMIT}^{{commit}}")
    old = tmp_path / "old"
    _extract(LEGACY, LEGACY_COMMIT, old)
    generated = subprocess.run(
        [sys.executable, "generate.py"], cwd=old, capture_output=True,
        env={"PATH": os.environ["PATH"], "_GENERATE_YAML_BOOTSTRAP": "1"}, text=True,
    )
    assert generated.returncode == 0, generated.stderr

    projection = tmp_path / "projection"
    compile_prompt_projection(LEGACY, LEGACY_COMMIT, projection, tmp_path / "installed")

    assert _outputs(projection) == _outputs(old)
    assert (projection / MANIFEST_NAME).is_file()


def _fixture_repository(tmp_path: Path, generator: str = "") -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    init_repo(repository, branch="main")
    _git(repository, "config", "commit.gpgsign", "false")
    (repository / "generate.py").write_text(generator or (
        "from pathlib import Path\n"
        "for name in ('agents/a.md','skills/s/SKILL.md','rules/r.md',"
        "'scopes/w/agents/pm.md','scopes/w/skills/w/SKILL.md'):\n"
        " p=Path(name); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(name+'\\n')\n"
        "Path('AGENTS.md').write_text('agents\\n')\n"
        "Path('instructions.md').write_text('instructions\\n')\n"
        "Path('org.json').write_text('{}\\n')\n"
    ))
    (repository / "paradigms.yaml").write_text("paradigms: {}\n")
    (repository / "toolsets.yaml").write_text("toolsets: {}\n")
    (repository / "hooks").mkdir()
    (repository / "hooks" / "hook.sh").write_text("#!/bin/sh\nexit 0\n")
    (repository / "hooks" / "hook.sh").chmod(0o755)
    (repository / "hooks" / "turn-end-asks.md").write_text("review before stop\n")
    (repository / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "prompt", "prompt": "placeholder"}]}]},
        "statusLine": {"type": "command", "command": "hook.sh"},
    }))
    commit_all(repository, "source")
    return repository


@pytest.mark.parametrize("unsafe", ["symlink", "case-collision", "submodule"])
def test_compiler_rejects_unsafe_committed_sources(tmp_path: Path, unsafe: str) -> None:
    repository = _fixture_repository(tmp_path)
    if unsafe == "symlink":
        (repository / "linked").symlink_to("generate.py")
        _git(repository, "add", "linked")
    elif unsafe == "case-collision":
        (repository / "GENERATE.py").write_text("collision\n")
        _git(repository, "add", "GENERATE.py")
    if unsafe == "submodule":
        # `commit_all`'s own `git add -A` would re-scan the working tree, find no `nested`
        # directory to back this gitlink, and stage its removal right back out — commit only
        # what `update-index` already staged, don't restage.
        commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "update-index", "--add", "--cacheinfo", "160000," + commit + ",nested")
        _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-qm", unsafe)
    else:
        commit_all(repository, unsafe)
    with pytest.raises(ValueError, match="unsafe committed source"):
        compile_prompt_projection(repository, "HEAD", tmp_path / "projection", tmp_path / "installed")


@pytest.mark.parametrize(
    ("generator", "message"),
    [
        (
            "from pathlib import Path\nimport os\n"
            "Path('agents').mkdir(); Path('agents/a.md').write_bytes(os.urandom(8))\n",
            "deterministic",
        ),
        (
            "from pathlib import Path\nPath('agents').mkdir(); "
            "Path('agents/a.md').symlink_to('/outside')\n",
            "non-regular",
        ),
        (
            "from pathlib import Path\nPath('agents').mkdir(); "
            "Path('agents/a.md').write_text('a'); Path('agents/A.md').write_text('b')\n",
            "collision",
        ),
    ],
    ids=["nondeterministic", "symlink", "case-collision"],
)
def test_compiler_rejects_unsafe_outputs(
    tmp_path: Path, generator: str, message: str
) -> None:
    repository = _fixture_repository(tmp_path, generator)
    with pytest.raises(ValueError, match=message):
        compile_prompt_projection(repository, "HEAD", tmp_path / "projection", tmp_path / "installed")


def test_compiler_output_is_derived_from_the_selected_commit(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    first = tmp_path / "first"
    compile_prompt_projection(repository, "HEAD", first, tmp_path / "installed")
    repeated = tmp_path / "repeated"
    compile_prompt_projection(repository, "HEAD", repeated, tmp_path / "another-install-root")
    assert (first / MANIFEST_NAME).read_bytes() == (repeated / MANIFEST_NAME).read_bytes()
    generator = repository / "generate.py"
    generator.write_text(generator.read_text().replace("agents\\n", "changed agents\\n"))
    commit_all(repository, "change source")
    second = tmp_path / "second"
    compile_prompt_projection(repository, "HEAD", second, tmp_path / "installed")
    assert (first / "AGENTS.md").read_bytes() != (second / "AGENTS.md").read_bytes()


def test_installer_stages_verified_files_without_touching_destination(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    projection = tmp_path / "projection"
    compile_prompt_projection(repository, "HEAD", projection, tmp_path / "installed")
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "keep").write_text("unchanged\n")

    staged = stage_projection_install(projection, installed, tmp_path / "install-stage")

    assert (installed / "keep").read_text() == "unchanged\n"
    assert not (staged / MANIFEST_NAME).exists()
    for path in staged.rglob("*"):
        if path.is_file():
            assert hashlib.sha256(path.read_bytes()).hexdigest()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_compiler_creates_a_fresh_cache_parent(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    projection = tmp_path / "fresh" / "cache" / _git(repository, "rev-parse", "HEAD")

    compile_prompt_projection(repository, "HEAD", projection, tmp_path / "installed")

    assert (projection / MANIFEST_NAME).is_file()


def test_installer_switches_only_declared_consumers_and_rejects_unmanaged_collision(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    projection = tmp_path / "projection"
    compile_prompt_projection(repository, "HEAD", projection, tmp_path / "installed")
    home = tmp_path / "home"
    vscode = tmp_path / "vscode"
    state = tmp_path / "state" / "installed.json"
    unrelated = home / ".claude" / "keep.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep\n")
    settings = home / ".claude" / "settings.json"
    unrelated_stop = {"matcher": "owner", "hooks": [{"type": "prompt", "prompt": "keep"}]}
    settings.write_text(json.dumps({
        "unrelated": True, "hooks": {"Stop": [unrelated_stop]},
    }) + "\n")

    install_prompt_projection(projection, home, vscode, state)

    assert (home / "AGENTS.md").read_text() == "agents\n"
    assert (home / ".codex" / "AGENTS.md").read_text() == "agents\n"
    assert not (home / "CLAUDE.md").exists()
    assert (home / ".claude" / "CLAUDE.md").read_text() == "instructions\n"
    assert (home / ".claude" / "scopes" / "w" / "agents" / "pm.md").is_file()
    assert (home / ".claude" / "scopes" / "w" / "skills" / "w" / "SKILL.md").is_file()
    assert not (home / ".claude" / "agents" / "pm.md").exists()
    assert (vscode / "alan.instructions.md").read_text() == "instructions\n"
    assert (vscode / "a.agent.md").is_file()
    assert (vscode / "s.skill.instructions.md").is_file()
    assert (vscode / "r.instructions.md").is_file()
    assert (home / ".claude" / "rules" / "r.md").is_file()
    assert unrelated.read_text() == "keep\n"
    assert json.loads(settings.read_text())["unrelated"] is True
    assert unrelated_stop in json.loads(settings.read_text())["hooks"]["Stop"]
    assert (home / ".claude" / "hooks" / "hook.sh").stat().st_mode & stat.S_IXUSR
    collision_home = tmp_path / "collision-home"
    collision = collision_home / "AGENTS.md"
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_text("unmanaged\n")
    with pytest.raises(ValueError, match="unmanaged"):
        install_prompt_projection(
            projection, collision_home, tmp_path / "collision-vscode",
            tmp_path / "collision-state.json",
        )


@pytest.mark.parametrize("matches", [True, False], ids=["exact", "mismatch"])
def test_installer_adopts_only_an_exact_declared_legacy_symlink(
    tmp_path: Path, matches: bool
) -> None:
    repository = _fixture_repository(tmp_path)
    projection = tmp_path / "projection"
    compile_prompt_projection(repository, "HEAD", projection, tmp_path / "installed")
    home, vscode = tmp_path / "home", tmp_path / "vscode"
    legacy = tmp_path / "legacy-agents"
    legacy.mkdir()
    source = legacy / "a.md"
    source.write_text("agents/a.md\n")
    target = home / ".claude" / "agents" / "a.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(source)
    adoption = tmp_path / "adoption.json"
    adoption.write_text(json.dumps({"version": 1, "targets": {str(target): {
        "symlink_target": str(source),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest() if matches else "0" * 64,
    }}}))

    if matches:
        install_prompt_projection(projection, home, vscode, tmp_path / "state.json", adoption)
        assert target.is_file() and not target.is_symlink()
    else:
        with pytest.raises(ValueError, match="adoption"):
            install_prompt_projection(projection, home, vscode, tmp_path / "state.json", adoption)


def test_installer_replaces_an_exact_adopted_skill_directory_symlink(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    projection = tmp_path / "projection"
    compile_prompt_projection(repository, "HEAD", projection, tmp_path / "installed")
    home, vscode = tmp_path / "home", tmp_path / "vscode"
    legacy = tmp_path / "legacy-skill"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("skills/s/SKILL.md\n")
    target = home / ".claude" / "skills" / "s"
    target.parent.mkdir(parents=True)
    target.symlink_to(legacy, target_is_directory=True)
    digest = hashlib.sha256(b"SKILL.md\0skills/s/SKILL.md\n").hexdigest()
    adoption = tmp_path / "adoption.json"
    adoption.write_text(json.dumps({"version": 1, "targets": {str(target): {
        "symlink_target": str(legacy), "sha256": digest,
    }}}))

    install_prompt_projection(projection, home, vscode, tmp_path / "state.json", adoption)

    assert target.is_dir() and not target.is_symlink()
    assert (target / "SKILL.md").read_text() == "skills/s/SKILL.md\n"


def test_full_recorded_consumer_topology_switches_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_root = Path(__file__).resolve().parents[1] / "out" / "tests" / (
        "20260905-prompt-local-first-sync-rp"
    )
    baseline = json.loads((evidence_root / "prompt-migration-baseline.json").read_text())
    hook_baseline = json.loads(
        (evidence_root / "prompt-migration-hooks-baseline.json").read_text()
    )
    assert len(baseline["consumer_entries"]) == 139
    repository = Path(baseline["source_repository"])
    projection = tmp_path / "projection"
    compile_prompt_projection(repository, baseline["source_commit"], projection, tmp_path / "unused")
    home = tmp_path / "home"
    vscode = home / ".config" / "Code" / "User" / "prompts"
    adoption_targets = {}
    source_hashes = {}
    entries = [*baseline["consumer_entries"], *hook_baseline["entries"]]
    recorded_home = Path(hook_baseline["settings"]["path"]).parent.parent
    for index, entry in enumerate(entries):
        original = Path(entry["path"])
        relative = original.relative_to(recorded_home)
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "directory":
            target.mkdir(exist_ok=True)
        elif entry["kind"] == "file":
            target.write_bytes(original.read_bytes())
        else:
            source = Path(entry["target"])
            source_hashes[source] = _path_digest(source)
            copied = tmp_path / "legacy" / str(index)
            copied.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, copied)
                digest = _path_digest(copied)
            else:
                copied.write_bytes(source.read_bytes())
                digest = hashlib.sha256(copied.read_bytes()).hexdigest()
            target.symlink_to(copied, target_is_directory=copied.is_dir())
            adoption_targets[str(target)] = {
                "symlink_target": str(copied), "sha256": digest,
            }
    settings = home / ".claude" / "settings.json"
    settings.write_bytes(Path(hook_baseline["settings"]["path"]).read_bytes())
    adoption = tmp_path / "adoption.json"
    adoption.write_text(json.dumps({"version": 1, "targets": adoption_targets}))
    state = tmp_path / "state.json"
    original_replace = os.replace
    failed = False

    def fail_state_once(source, destination, **kwargs):
        nonlocal failed
        actual_destination = (Path(os.readlink(f"/proc/self/fd/{kwargs['dst_dir_fd']}")) / destination
                              if "dst_dir_fd" in kwargs else Path(destination))
        if actual_destination == state and not failed:
            failed = True
            raise OSError("injected state switch failure")
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr("interact.prompt_projection.os.replace", fail_state_once)
    with pytest.raises(OSError, match="injected"):
        install_prompt_projection(projection, home, vscode, state, adoption)
    monkeypatch.setattr("interact.prompt_projection.os.replace", original_replace)
    install_prompt_projection(projection, home, vscode, state, adoption)

    managed = json.loads(state.read_text())["managed"]
    assert len(managed) > 100
    assert (home / ".interact" / "agents.json").read_bytes() == Path(
        next(entry["path"] for entry in entries if entry["path"].endswith("/.interact/agents.json"))
    ).read_bytes()
    assert all(_path_digest(path) == digest for path, digest in source_hashes.items())


def _path_digest(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_file():
            digest.update(child.relative_to(path).as_posix().encode() + b"\0" + child.read_bytes())
    return digest.hexdigest()
