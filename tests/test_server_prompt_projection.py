"""Server projections preserve pinned content and never replace runtime hook ownership."""

import errno
import hashlib
import json
import os
import select
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from interact_core import AgentCatalogSnapshot, AgentRevision, AgentRevisionRef, PromptExecutionRef, PromptKey, PromptRevision

from interact.agents.catalog import AgentCatalog, CatalogSnapshot
from interact.agents.catalog_connection import CatalogAuthenticationError, CatalogConnection
from interact.pinned_directory import PathDirectory
from interact.prompt_projection import (
    MANIFEST_NAME, _server_outputs, compile_server_prompt_projection, install_prompt_projection,
    install_server_prompt_projection,
)

pytestmark = pytest.mark.usefixtures("directory_backend")


def catalog_fixture(skill_description=True, prompt_headers=None, worker_tools=()):
    contents = {
        'main': 'Main charter.', 'worker': 'Worker charter.',
        'provider-openai': 'OpenAI provider context.',
        'provider-anthropic': 'Anthropic provider context.',
        'skill': ('---\nname: skill\ndescription: Use for fixture work.\ninteract:\n  projection: skill\n  scope: domain\n---\n\n' if skill_description else '') + 'Pinned skill body.',
        'rule': '---\nname: rule\ndescription: Fixture rule.\ninteract:\n  projection: rule\n---\n\nRule body.',
    }
    for slug, header in (prompt_headers or {}).items():
        contents[slug] = contents[slug].replace("---\n", "---\n" + yaml.safe_dump(header), 1)
    prompts = {key: PromptRevision(key=PromptKey(namespace='paradigms', slug=key),
        revision=uuid4(), content=value, digest=hashlib.sha256(value.encode()).hexdigest(),
        source_commit='a' * 40, created_at=datetime.now(UTC)) for key, value in contents.items()}
    refs = {key: PromptExecutionRef(key=p.key, revision=p.revision, digest=p.digest, channel='stable')
            for key, p in prompts.items()}
    main = AgentRevision(id=uuid4(), revision=uuid4(), name='Main', role_key='main',
        prompt=refs['main'], paradigms=(refs['provider-anthropic'],), skill_paradigms=(refs['skill'],),
        resources=(), created_at=datetime.now(UTC))
    worker = AgentRevision(id=uuid4(), revision=uuid4(), name='Worker', role_key='worker',
        scope='domain', reports_to=main.id, prompt=refs['worker'], skill_paradigms=(refs['skill'],),
        harness_tools=tuple(worker_tools), resources=(), created_at=datetime.now(UTC))
    snapshot = AgentCatalogSnapshot.create((main, worker), tuple(prompts.values()), AgentRevisionRef(id=main.id, revision=main.revision), tuple(refs.values()))
    return AgentCatalog(connection=CatalogConnection(endpoint='http://127.0.0.1:8817', auth_mode='preview',
        workspace_id=uuid4()), snapshot=CatalogSnapshot.model_validate(snapshot.model_dump()),
        fetched_at=datetime.now(UTC))


def stage(outputs, root):
    records = []
    for name, content in outputs.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o600)
        records.append({'path': name, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                        'size': len(content.encode()), 'mode': '0600'})
    (root / MANIFEST_NAME).write_text(json.dumps({'source_kind': 'server-catalog',
        'source_commit': None, 'catalog_cursor': 'fixture', 'outputs': records}))


def test_provider_context_scope_and_immutable_skill_references(tmp_path):
    outputs = _server_outputs(catalog_fixture(), tmp_path / 'home', tmp_path / 'projection')
    assert 'OpenAI provider context.' in outputs['AGENTS.md']
    assert 'Anthropic provider context.' not in outputs['AGENTS.md']
    assert 'Anthropic provider context.' in outputs['instructions.md']
    assert 'OpenAI provider context.' not in outputs['instructions.md']
    assert 'scopes/domain/agents/worker.md' in outputs
    assert 'scopes/domain/skills/skill/SKILL.md' in outputs
    assert 'skills/skill/SKILL.md' not in outputs
    worker = outputs['scopes/domain/agents/worker.md']
    assert 'Pinned skill body.' not in worker
    reference = next(name for name in outputs if name.startswith('references/'))
    assert str(tmp_path / 'projection' / reference) in worker
    assert '/RULE.md' in outputs['AGENTS.md']
    org = json.loads(outputs['org.json'])
    assert org['agents'][0]['reports_to'] == 'main'
    assert org['agents'][0]['def'] == str(tmp_path / 'home/.claude/scopes/domain/agents/worker.md')


def test_missing_skill_metadata_refuses_projection(tmp_path):
    with pytest.raises(ValueError, match='lacks routing metadata'):
        _server_outputs(catalog_fixture(False), tmp_path, tmp_path / 'projection')


@pytest.mark.parametrize("slug", ["skill", "rule"])
@pytest.mark.parametrize("field,value", [
    ("hooks", {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo forbidden"}]}]}),
    ("allowed-tools", ["Bash"]), ("context", "fork"), ("agent", "worker"),
    ("model", "vendor/example-model"), ("disable-model-invocation", True),
    ("user-invocable", True), ("permissions", {"allow": ["Bash"]}),
    ("future-runtime-field", "unknown"),
])
def test_server_runtime_frontmatter_refuses_before_activation(tmp_path, monkeypatch, slug, field, value):
    catalog = catalog_fixture(prompt_headers={slug: {field: value}})
    monkeypatch.setattr(AgentCatalog, "refresh", lambda *args, **kwargs: catalog)
    monkeypatch.setattr(CatalogConnection, "path", classmethod(lambda cls: tmp_path / "connection.json"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    with pytest.raises(ValueError, match="metadata"):
        install_server_prompt_projection(catalog.connection, tmp_path / "home", tmp_path / "vscode",
                                         tmp_path / "state/installed.json")
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "vscode").exists()
    assert not (tmp_path / "state").exists()
    assert not list((tmp_path / "cache").rglob(MANIFEST_NAME))


@pytest.mark.parametrize("slug", ["skill", "rule"])
@pytest.mark.parametrize("malicious", ["pinned", "head"])
def test_each_exact_revision_and_discovery_head_requires_safe_metadata(tmp_path, slug, malicious):
    catalog = catalog_fixture(prompt_headers={slug: {"allowed-tools": ["Bash"]}} if malicious == "pinned" else {})
    old = next(prompt for prompt in catalog.snapshot.paradigms if prompt.key.slug == slug)
    content = (old.content.replace("allowed-tools:\n- Bash\n", "") if malicious == "pinned"
               else old.content.replace("---\n", "---\nallowed-tools: [Bash]\n", 1))
    new = old.model_copy(update={"revision": uuid4(), "content": content,
                                "digest": hashlib.sha256(content.encode()).hexdigest()})
    heads = tuple(reference for reference in catalog.snapshot.prompt_heads if reference.key != old.key) + (
        PromptExecutionRef(key=new.key, revision=new.revision, digest=new.digest, channel="stable"),)
    snapshot = AgentCatalogSnapshot.create(catalog.snapshot.agents, (*catalog.snapshot.paradigms, new),
                                           catalog.snapshot.root_agent, heads)
    catalog = catalog.model_copy(update={"snapshot": CatalogSnapshot.model_validate(snapshot.model_dump())})
    with pytest.raises(ValueError, match="metadata"):
        _server_outputs(catalog, tmp_path / "home", tmp_path / "projection")


def test_canonical_skill_and_rule_metadata_compiles_and_installs_unchanged(tmp_path, monkeypatch):
    source = Path.home() / ".local/share/interact/prompts"
    if not (source / "paradigms.yaml").is_file():
        pytest.skip("canonical prompt working copy is unavailable")
    entries = yaml.safe_load((source / "paradigms.yaml").read_text())["paradigms"]
    catalog = catalog_fixture()
    records = [prompt for prompt in catalog.snapshot.paradigms if prompt.key.slug not in {"skill", "rule"}]
    skills, expected = [], {}
    for name, config in entries.items():
        if not (config.get("skill") or config.get("rules")):
            continue
        kind = "skill" if config.get("skill") else "rule"
        content = (source / "paradigms" / f"{name}.md").read_text()
        routing = {"projection": kind, "scope": config.get("scope", "core")}
        if config.get("paths"):
            routing["paths"] = config["paths"]
        # Add server routing to an in-memory copy; canonical sources are never edited.
        content = content.replace("---\n", "---\n" + yaml.safe_dump({"interact": routing}), 1)
        prompt = PromptRevision(key=PromptKey(namespace="paradigms", slug=name), revision=uuid4(),
                                content=content, digest=hashlib.sha256(content.encode()).hexdigest(),
                                source_commit="a" * 40, created_at=datetime.now(UTC))
        records.append(prompt)
        if kind == "skill":
            skills.append(PromptExecutionRef(key=prompt.key, revision=prompt.revision, digest=prompt.digest, channel="stable"))
        expected[f"references/paradigms/{name}/{prompt.digest}/{kind.upper()}.md"] = content
    assert len(skills) == 15 and len(expected) == 20
    agents = tuple(agent.model_copy(update={"skill_paradigms": tuple(skills)}) for agent in catalog.snapshot.agents)
    heads = tuple(PromptExecutionRef(key=prompt.key, revision=prompt.revision, digest=prompt.digest, channel="stable") for prompt in records)
    snapshot = AgentCatalogSnapshot.create(agents, tuple(records), catalog.snapshot.root_agent, heads)
    catalog = catalog.model_copy(update={"snapshot": CatalogSnapshot.model_validate(snapshot.model_dump())})
    monkeypatch.setattr(AgentCatalog, "refresh", lambda *args, **kwargs: catalog)
    monkeypatch.setattr(CatalogConnection, "path", classmethod(lambda cls: tmp_path / "connection.json"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    projection = install_server_prompt_projection(catalog.connection, tmp_path / "home", tmp_path / "vscode", tmp_path / "state.json")
    for relative, content in expected.items():
        assert (projection / relative).read_bytes() == content.encode()
    assert (tmp_path / "home/.claude/skills/coding/SKILL.md").read_bytes() == (projection / "skills/coding/SKILL.md").read_bytes()
    for name, config in entries.items():
        if config.get("skill") and config.get("scope", "core") != "core":
            assert (tmp_path / f"home/.claude/scopes/{config['scope']}/skills/{name}/SKILL.md").is_file()


@pytest.mark.parametrize("relative", [
    "skills/example/SKILL.md", "scopes/domain/skills/example/SKILL.md",
    "references/paradigms/example/digest/SKILL.md", "rules/example.md",
    "scopes/domain/rules/example.md", "references/paradigms/example/digest/RULE.md",
])
def test_server_manifest_cannot_activate_runtime_headers_in_any_copy(tmp_path, relative):
    projection = tmp_path / "projection"
    stage({"AGENTS.md": "safe root", relative: "---\nname: example\ndescription: Example.\nallowed-tools: [Bash]\n---\nBody."}, projection)
    with pytest.raises(ValueError, match="metadata"):
        install_prompt_projection(projection, tmp_path / "home", tmp_path / "vscode", tmp_path / "state.json")
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "vscode").exists()
    assert not (tmp_path / "state.json").exists()


def test_prompt_only_install_retains_hooks_settings_and_local_edits(tmp_path):
    home, projection = tmp_path / 'home', tmp_path / 'projection'
    state = tmp_path / 'state/installed.json'
    runtime = {home / '.claude/hooks/guard.sh': '#!/bin/sh\nexit 0\n',
               home / '.claude/settings.json': '{"unrelated":true}',
               home / '.codex/hooks.json': '{"hooks":{}}'}
    old_agent = home / '.claude/agents/obsolete.md'
    managed = {}
    for target, content in {**runtime, old_agent: 'old managed role'}.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        managed[str(target)] = hashlib.sha256(content.encode()).hexdigest()
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'managed': managed, 'hook_groups': {'Start': ['unchanged']}}))
    outputs = _server_outputs(catalog_fixture(), home, projection)
    stage(outputs, projection)
    install_prompt_projection(projection, home, home / 'vscode', state)
    assert not old_agent.exists()
    assert (home / '.codex/AGENTS.md').read_text() == outputs['AGENTS.md']
    for target, content in runtime.items():
        assert target.read_text() == content
    installed = json.loads(state.read_text())
    assert installed['hook_groups'] == {'Start': ['unchanged']}
    assert installed['source_kind'] == 'server-catalog'
    assert all(str(target) in installed['managed'] for target in runtime)
    (home / '.codex/AGENTS.md').write_text('operator edit')
    with pytest.raises(ValueError, match='changed locally'):
        install_prompt_projection(projection, home, home / 'vscode', state)
    assert (home / '.codex/AGENTS.md').read_text() == 'operator edit'


def test_rule_heads_are_explicit_scoped_and_revision_safe(tmp_path):
    catalog = catalog_fixture()
    old = next(value for value in catalog.snapshot.paradigms if value.key.slug == "rule")
    content = old.content.replace("projection: rule", "projection: rule\n  scope: special\n  paths: [src/**]").replace("Rule body.", "New rule.")
    new = old.model_copy(update={"revision": uuid4(), "content": content,
                                "digest": hashlib.sha256(content.encode()).hexdigest()})
    heads = tuple(reference for reference in catalog.snapshot.prompt_heads if reference.key != old.key) + (
        PromptExecutionRef(key=new.key, revision=new.revision, digest=new.digest, channel="stable"),)
    # Timestamp deliberately unchanged: heads, never list order or timestamps, select discovery.
    prompts = (*catalog.snapshot.paradigms, new)
    for values in (prompts, tuple(reversed(prompts))):
        snapshot = AgentCatalogSnapshot.create(catalog.snapshot.agents, values,
                                               catalog.snapshot.root_agent, heads)
        revised = catalog.model_copy(update={"snapshot": CatalogSnapshot.model_validate(snapshot.model_dump())})
        outputs = _server_outputs(revised, tmp_path, tmp_path / "cache")
        assert "rules/rule.md" not in outputs
        assert "New rule." in outputs["scopes/special/rules/rule.md"]
        assert "paths:\n- src/**" in outputs["scopes/special/rules/rule.md"]
        assert "special/rules" not in outputs["AGENTS.md"]
        assert outputs[f"references/paradigms/rule/{old.digest}/RULE.md"] == old.content
        assert outputs[f"references/paradigms/rule/{new.digest}/RULE.md"] == new.content


def test_custom_root_and_child_project_without_inventing_role_policy(tmp_path):
    catalog = catalog_fixture()
    agents = tuple(agent.model_copy(update={"role_key": None}) for agent in catalog.snapshot.agents)
    snapshot = AgentCatalogSnapshot.create(agents, catalog.snapshot.paradigms,
                                           catalog.snapshot.root_agent, catalog.snapshot.prompt_heads)
    revised = catalog.model_copy(update={"snapshot": CatalogSnapshot.model_validate(snapshot.model_dump())})
    outputs = _server_outputs(revised, tmp_path, tmp_path / "cache")
    org = json.loads(outputs["org.json"])
    assert org["coordinator"]["id"] == f"agent-{snapshot.root_agent.id}"
    assert len(org["agents"]) == 1
    assert "Main charter." in outputs["AGENTS.md"]
    assert "criteria" not in org["agents"][0]


@pytest.mark.parametrize("when", ["before_move", "after_move", "rollback", "rollback_move"])
def test_intervening_operator_write_is_never_overwritten(tmp_path, monkeypatch, when):
    home, projection = tmp_path / "home", tmp_path / "projection"
    state, vscode = tmp_path / "state/installed.json", tmp_path / "vscode"
    stage(_server_outputs(catalog_fixture(), home, projection), projection)
    install_prompt_projection(projection, home, vscode, state)
    previous_state = state.read_bytes()
    target = home / "AGENTS.md"
    original = target.read_bytes()
    outputs = _server_outputs(catalog_fixture(), home, projection)
    outputs["AGENTS.md"] = "updated projection\n"
    stage(outputs, projection)
    replace = os.replace
    injected = False

    def intervene(source, destination, **kwargs):
        nonlocal injected
        actual_source = (Path(os.readlink(f"/proc/self/fd/{kwargs['src_dir_fd']}")) / source
                         if "src_dir_fd" in kwargs else Path(source))
        actual_destination = (Path(os.readlink(f"/proc/self/fd/{kwargs['dst_dir_fd']}")) / destination
                              if "dst_dir_fd" in kwargs else Path(destination))
        if actual_source == target and when == "rollback_move" and injected:
            target.write_text("operator edit")
            return replace(source, destination, **kwargs)
        if actual_source == target and not injected and when in {"before_move", "after_move"}:
            injected = True
            if when == "before_move":
                target.write_text("operator edit")
            result = replace(source, destination, **kwargs)
            if when == "after_move":
                target.write_text("operator edit")
            return result
        if actual_destination == state and when in {"rollback", "rollback_move"}:
            injected = True
            if when == "rollback":
                target.write_text("operator edit")
            raise OSError("state publication failed")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", intervene)
    with pytest.raises((ValueError, OSError)):
        install_prompt_projection(projection, home, vscode, state)
    assert injected
    assert target.read_text() == "operator edit"
    assert state.read_bytes() == previous_state
    assert (home / ".codex/AGENTS.md").read_bytes() == original
    transaction = state.parent / ".prompt-install-transaction"
    if when == "before_move":
        assert not transaction.exists()
    else:
        assert any(path.read_bytes() == original for path in transaction.glob("backup-*"))
        assert (transaction / "recovery.json").is_file()
        with pytest.raises(ValueError, match="recovery"):
            install_prompt_projection(projection, home, vscode, state)


def test_install_copies_where_the_file_system_has_no_hard_links(tmp_path, monkeypatch, directory_backend):
    if directory_backend is not PathDirectory:
        pytest.skip("the POSIX backend publishes by hard link only")
    home, projection = tmp_path / "home", tmp_path / "projection"
    state, vscode = tmp_path / "state/installed.json", tmp_path / "vscode"

    def no_hard_links(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "this file system has no hard links")

    monkeypatch.setattr(os, "link", no_hard_links)
    for revision in ("first", "second"):
        outputs = _server_outputs(catalog_fixture(), home, projection)
        outputs["AGENTS.md"] = f"{revision} projection\n"
        stage(outputs, projection)
        install_prompt_projection(projection, home, vscode, state)
        assert [(home / name).read_text() for name in ("AGENTS.md", ".codex/AGENTS.md")] == [f"{revision} projection\n"] * 2
    assert not (state.parent / ".prompt-install-transaction").exists()


@pytest.mark.parametrize("race", [False, True])
def test_server_cache_compares_verified_content_on_every_hit(tmp_path, monkeypatch, race):
    catalog = catalog_fixture()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(AgentCatalog, "refresh", lambda *args, **kwargs: catalog)
    home = tmp_path / "home"
    key = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]
    destination = (tmp_path / "cache/interact/prompts/server" / str(catalog.connection.workspace_id)
                   / f"{catalog.snapshot.cursor}-{key}-{catalog.access_generation}")

    def corrupt():
        outputs = _server_outputs(catalog, home, destination)
        outputs["AGENTS.md"] = "forged self-consistent cache"
        stage(outputs, destination)

    @contextmanager
    def guard(*args, **kwargs):
        if race:
            corrupt()
        yield catalog.access_generation

    monkeypatch.setattr(CatalogConnection, "access_guard", guard)
    if not race:
        corrupt()
    with pytest.raises(ValueError, match="differs from verified"):
        compile_server_prompt_projection(catalog.connection, home)


def test_server_cache_rejects_symlink_ancestor_before_writing(tmp_path, monkeypatch):
    catalog = catalog_fixture()
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "interact").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setattr(AgentCatalog, "refresh", lambda *args, **kwargs: catalog)
    with pytest.raises(ValueError, match="safe directory"):
        compile_server_prompt_projection(catalog.connection, tmp_path / "home")
    assert list(outside.iterdir()) == []


def test_install_consumes_current_rule_paths_and_holds_domain_outside_autoload(tmp_path):
    catalog = catalog_fixture()
    old = next(prompt for prompt in catalog.snapshot.paradigms if prompt.key.slug == "rule")
    content = old.content.replace("projection: rule", "projection: rule\n  paths: [src/**, tests/**]").replace("Rule body.", "Current rule.")
    new = old.model_copy(update={"revision": uuid4(), "content": content,
                                "digest": hashlib.sha256(content.encode()).hexdigest()})
    heads = tuple(ref for ref in catalog.snapshot.prompt_heads if ref.key != old.key) + (
        PromptExecutionRef(key=new.key, revision=new.revision, digest=new.digest, channel="stable"),)
    snapshot = AgentCatalogSnapshot.create(catalog.snapshot.agents, (*catalog.snapshot.paradigms, new),
                                           catalog.snapshot.root_agent, heads)
    catalog = catalog.model_copy(update={"snapshot": CatalogSnapshot.model_validate(snapshot.model_dump())})
    home, projection, vscode = tmp_path / "home", tmp_path / "projection", tmp_path / "vscode"
    outputs = _server_outputs(catalog, home, projection)
    stage(outputs, projection)
    install_prompt_projection(projection, home, vscode, tmp_path / "state.json")
    claude = (home / ".claude/rules/rule.md").read_text()
    code = (vscode / "rule.instructions.md").read_text()
    assert yaml.safe_load(claude.split("---")[1])["paths"] == ["src/**", "tests/**"]
    assert yaml.safe_load(code.split("---")[1])["applyTo"] == "src/**,tests/**"
    assert "Current rule." in claude and "Rule body." not in claude
    assert (projection / f"references/paradigms/rule/{old.digest}/RULE.md").read_text() == old.content
    assert (projection / f"references/paradigms/rule/{new.digest}/RULE.md").read_text() == new.content
    assert (home / ".claude/scopes/domain/agents/worker.md").is_file()
    assert (home / ".claude/scopes/domain/skills/skill/SKILL.md").is_file()
    assert not (home / ".claude/agents/worker.md").exists()
    assert not (home / ".codex/skills/skill").exists()
    assert not (vscode / "worker.agent.md").exists()
    assert not (vscode / "skill.skill.instructions.md").exists()


def test_missing_authoritative_heads_requires_server_upgrade(tmp_path):
    catalog = catalog_fixture()
    catalog = catalog.model_copy(update={"snapshot": catalog.snapshot.model_copy(update={"prompt_heads": ()})})
    with pytest.raises(ValueError, match="authoritative prompt heads; upgrade the server"):
        _server_outputs(catalog, tmp_path, tmp_path / "projection")


@pytest.mark.parametrize("denial", ["refresh", "generation"])
def test_denied_server_install_never_writes_consumer_state(tmp_path, monkeypatch, denial):
    catalog = catalog_fixture()
    monkeypatch.setattr(CatalogConnection, "path", classmethod(lambda cls: tmp_path / "connection.json"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    if denial == "refresh":
        def denied(*args, **kwargs):
            raise CatalogAuthenticationError("access denied")
        monkeypatch.setattr(AgentCatalog, "refresh", denied)
    else:
        monkeypatch.setattr(AgentCatalog, "refresh", lambda *args, **kwargs: catalog)
        compiled = compile_server_prompt_projection(catalog.connection, tmp_path / "home")
        with catalog.connection.access_guard():
            generation = catalog.connection.session_path().with_suffix(".generation")
            generation.write_text(str(uuid4()))
            generation.chmod(0o600)
        monkeypatch.setattr("interact.prompt_projection.compile_server_prompt_projection", lambda *args: compiled)
    with pytest.raises(CatalogAuthenticationError):
        install_server_prompt_projection(catalog.connection, tmp_path / "home", tmp_path / "vscode", tmp_path / "state/installed.json")
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "vscode").exists()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("unsafe", ["consumer_parent", "vscode_root", "recorded_target"])
def test_install_rejects_paths_outside_consumer_roots(tmp_path, unsafe):
    home, projection, vscode = tmp_path / "home", tmp_path / "projection", tmp_path / "vscode"
    state = tmp_path / "state.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "CLAUDE.md"
    protected.write_text("operator content")
    stage({"instructions.md": "new instructions"}, projection)
    if unsafe == "consumer_parent":
        home.mkdir()
        (home / ".claude").symlink_to(outside, target_is_directory=True)
    elif unsafe == "vscode_root":
        vscode.symlink_to(outside, target_is_directory=True)
    else:
        state.write_text(json.dumps({"managed": {str(protected): hashlib.sha256(protected.read_bytes()).hexdigest()}}))
    with pytest.raises(ValueError, match="safe directory|escapes its root|adoption manifest"):
        install_prompt_projection(projection, home, vscode, state)
    assert protected.read_text() == "operator content"
    assert list(outside.iterdir()) == [protected]


def test_independent_connection_processes_lock_before_reading_shared_install_state(tmp_path):
    home, vscode, state = tmp_path / "home", tmp_path / "vscode", tmp_path / "state/installed.json"
    projections = [tmp_path / name for name in ("first", "second")]
    for index, projection in enumerate(projections):
        stage({"AGENTS.md": f"revision {index}"}, projection)
        manifest = json.loads((projection / MANIFEST_NAME).read_text())
        manifest["access_generation"] = "00000000-0000-0000-0000-000000000000"
        (projection / MANIFEST_NAME).write_text(json.dumps(manifest))
    script = """
import sys
from pathlib import Path
from uuid import UUID
from interact import prompt_projection as projection
from interact.agents.catalog_connection import CatalogConnection
root, source, ordinal = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
CatalogConnection.path = classmethod(lambda cls: root / 'connection.json')
connection = CatalogConnection(endpoint=f'http://127.0.0.1:{8817 + ordinal}',
    auth_mode='preview', workspace_id=UUID(int=ordinal + 1))
projection.compile_server_prompt_projection = lambda *args: source
read_state = projection._installed_document
def observed_read(path):
    result = read_state(path)
    print('READ', flush=True)
    if ordinal == 0:
        sys.stdin.readline()
    return result
projection._installed_document = observed_read
print('START', flush=True)
projection.install_server_prompt_projection(connection, root / 'home', root / 'vscode',
    root / 'state/installed.json')
print('DONE', flush=True)
"""
    processes = []
    try:
        for index, projection in enumerate(projections):
            process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(projection), str(index)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True)
            processes.append(process)
            assert select.select([process.stdout], [], [], 15)[0], "installer did not start"
            assert process.stdout.readline().strip() == "START"
            if index == 0:
                assert process.stdout.readline().strip() == "READ"
            else:
                assert not select.select([process.stdout], [], [], 0.3)[0], "second installer read stale state"
        processes[0].stdin.write("continue\n")
        processes[0].stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            assert "DONE" in stdout
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert (home / "AGENTS.md").read_text() == "revision 1"
    assert (home / ".codex/AGENTS.md").read_text() == "revision 1"
    managed = json.loads(state.read_text())["managed"]
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in managed.items())
    assert not (state.parent / ".prompt-install-transaction").exists()


def _worker_header(outputs):
    return yaml.safe_load(outputs['scopes/domain/agents/worker.md'].split('---\n')[1])


def test_projection_lists_only_tools_this_server_can_dispatch(tmp_path):
    """A listed tool is a callable tool: the projection resolves every `mcp__interact__…` name
    against the registry `call_tool` reads, and refuses the install when one is missing (#224 —
    a role was told about tools its session could not dispatch, and read "No such tool
    available" mid-task)."""
    from interact.server import dispatchable_tools

    dispatchable = sorted(dispatchable_tools())
    assert {'navigate', 'screenshot', 'session'} <= set(dispatchable)
    listed = ['Read', *(f'mcp__interact__{name}' for name in dispatchable)]
    outputs = _server_outputs(catalog_fixture(worker_tools=listed), tmp_path / 'home', tmp_path / 'projection')
    assert _worker_header(outputs)['tools'] == listed

    with pytest.raises(ValueError, match=r"cannot dispatch: mcp__interact__teleport"):
        _server_outputs(catalog_fixture(worker_tools=['Read', 'mcp__interact__teleport']),
                        tmp_path / 'home', tmp_path / 'projection')


def test_projection_leaves_another_server_s_tools_alone(tmp_path):
    """Only interact's own prefix is ours to judge — a foreign MCP tool is listed as written."""
    listed = ['Bash', 'mcp__other__navigate', 'mcp__interact__navigate']
    outputs = _server_outputs(catalog_fixture(worker_tools=listed), tmp_path / 'home', tmp_path / 'projection')
    assert _worker_header(outputs)['tools'] == listed
