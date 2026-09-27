"""Configured prompt editor uses live workspace heads and preserves local recovery."""

import hashlib
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from interact_core.accounts import Account, Bootstrap, Workspace
from interact_core.prompts import PromptCreateRequest, PromptKey, PromptRevision

from interact.agents.catalog_connection import CatalogConnection
from interact.cli import prompts
from interact.config import UserConfig
from interact.server_prompts import MAX_EDITOR_BYTES, ServerPrompts


@pytest.fixture
def remote_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config" / "config.env")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    connection.save()
    source = tmp_path / "data" / "interact" / "prompts" / "fixture" / "primary.md"
    source.parent.mkdir(parents=True)
    source.write_text("precious unpublished local work\n")
    head = PromptRevision(
        key=PromptKey(namespace="fixture", slug="primary"), name="Fixture prompt", revision=uuid4(),
        content="server current head", digest=hashlib.sha256(b"server current head").hexdigest(),
        source_commit="a" * 40, created_at=datetime.now(UTC),
    )
    state = {"head": head, "requests": [], "failure": None, "source": source, "connection": connection,
             "race": False, "invalidate": False, "catalog": None, "response": None,
             "token": uuid4().hex}
    bootstrap = Bootstrap(
        account=Account(account_id=uuid4(), email="fixture@example.invalid", locale="en", verified=True),
        workspaces=(Workspace(workspace_id=connection.workspace_id, name="Fixture", role="owner"),),
        current_workspace_id=connection.workspace_id, csrf_token="synthetic-csrf",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    def respond(request):
        state["requests"].append(request)
        if state["failure"] == "offline":
            raise httpx.ConnectError("synthetic offline", request=request)
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=synthetic; Path=/"}, json={})
        if "Authorization" in request.headers:
            assert request.headers["Authorization"] == f"Bearer {state['token']}"
            assert request.method == "GET" and "Cookie" not in request.headers
        else:
            assert request.headers["Cookie"] == "session=synthetic"
        if state["failure"] in {401, 403}:
            return httpx.Response(state["failure"])
        if request.url.path == "/v1/bootstrap":
            return httpx.Response(200, content=bootstrap.model_dump_json())
        assert not request.url.params
        if request.url.path.endswith("/sync-status"):
            return httpx.Response(200, json={"server_head": state["head"].source_commit})
        if request.method == "GET":
            if state["invalidate"]:
                connection.invalidate_access(httpx.Client(transport=httpx.MockTransport(respond)))
            if request.url.path.endswith("/prompts"):
                values = state["catalog"]
                return httpx.Response(200, json=values if values is not None else [state["head"].model_dump(mode="json")])
            if state["response"] is not None:
                return httpx.Response(200, content=state["response"])
            return httpx.Response(200, content=state["head"].model_dump_json())
        if request.method == "POST":
            assert request.url.path == f"/v1/workspaces/{connection.workspace_id}/prompts"
            assert request.headers["x-csrf-token"] == bootstrap.csrf_token
            assert request.headers["Origin"] == connection.endpoint
            proposed = PromptCreateRequest.model_validate_json(request.content)
            if state["failure"] == "post-forbidden":
                return httpx.Response(403)
            if proposed.key == state["head"].key or state["race"]:
                return httpx.Response(409, json={"code": "prompt_conflict"})
            if state["response"] is not None:
                return httpx.Response(201, content=state["response"])
            state["head"] = PromptRevision(key=proposed.key, name=proposed.name, content=proposed.content,
                digest=hashlib.sha256(proposed.content.encode()).hexdigest(), revision=uuid4(),
                source_commit="b" * 40, created_at=datetime.now(UTC))
            return httpx.Response(201, content=state["head"].model_dump_json())
        assert request.method == "PUT"
        assert request.headers["x-csrf-token"] == bootstrap.csrf_token
        assert request.headers["Origin"] == connection.endpoint
        proposed = PromptRevision.model_validate_json(request.content)
        if state["failure"] == "put-forbidden":
            return httpx.Response(403)
        if state["race"] or proposed.parent_digest != state["head"].digest:
            return httpx.Response(409)
        state["head"] = proposed.model_copy(update={"revision": uuid4(), "source_commit": "b" * 40, "parent_digest": None})
        return httpx.Response(200, content=state["head"].model_dump_json())

    connect = CatalogConnection.connect
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: connect(self, transport=transport))
    return state


def invoke(capsys, *arguments, content=""):
    original = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(content.encode("utf-8")))
    try:
        try:
            prompts.prompts_app(list(arguments))
            code = 0
        except SystemExit as error:
            code = error.code
    finally:
        sys.stdin = original
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_configured_editor_changes_current_remote_head_preserves_local_file(remote_prompts, capsys):
    state = remote_prompts
    before = state["source"].read_bytes()
    assert invoke(capsys, "catalog") == (0, {"ok": True, "files": ["fixture/primary.md"]})
    code, read = invoke(capsys, "read", "fixture/primary.md")
    assert code == 0 and read == {"ok": True, "path": "fixture/primary.md", "content": state["head"].content,
                                "digest": state["head"].digest}
    code, saved = invoke(capsys, "write", "fixture/primary.md", read["digest"], content="my edited buffer\n")
    assert code == 0 and saved == {"ok": True, "path": "fixture/primary.md",
                                 "digest": hashlib.sha256(b"my edited buffer\n").hexdigest()}
    assert state["head"].content == "my edited buffer\n"
    submitted = next(request for request in state["requests"] if request.method == "PUT")
    assert PromptRevision.model_validate_json(submitted.content).parent_digest == read["digest"]
    assert invoke(capsys, "read", "fixture/primary.md")[1]["content"] == "my edited buffer\n"
    assert state["source"].read_bytes() == before
    assert len([r for r in state["requests"] if r.method == "PUT"]) == 1
    assert invoke(capsys, "write", "fixture/primary.md", saved["digest"], content="my edited buffer\n")[0] == 0
    assert len([r for r in state["requests"] if r.method == "PUT"]) == 1


@pytest.mark.parametrize("failure", ["stale", "race", 401, 403, "put-forbidden", "offline"])
def test_failed_save_never_writes_local_or_reports_success(remote_prompts, capsys, failure):
    state = remote_prompts
    head = state["head"]
    state["failure"] = failure
    state["race"] = failure == "race"
    digest = "0" * 64 if failure == "stale" else head.digest
    code, result = invoke(capsys, "write", "fixture/primary.md", digest, content="keep my editor input")
    assert code == 2 and result["ok"] is False
    assert "preserve the editor buffer" in result["error"]
    assert result["code"] == ("conflict" if failure in {"stale", "race"} else "invalid")
    assert state["head"] == head
    assert state["source"].read_text() == "precious unpublished local work\n"
    if failure in {401, 403, "put-forbidden"}:
        assert not state["connection"].session_path().exists()
        assert state["connection"].access_generation().int != 0


@pytest.mark.parametrize("command", ["catalog", "read", "status", "diff", "log"])
@pytest.mark.parametrize("failure", [401, 403, "offline"])
def test_failed_reads_never_fall_back_to_local(remote_prompts, capsys, command, failure):
    remote_prompts["failure"] = failure
    args = (command, "fixture/primary.md") if command == "read" else (command,)
    code, result = invoke(capsys, *args)
    assert code == 2 and result["ok"] is False
    assert "precious" not in json.dumps(result)


@pytest.mark.parametrize("path", ["../primary.md", "/fixture/primary.md", "fixture/../primary.md",
    "fixture//primary.md", "fixture/./primary.md", "fixture/primary.MD", "fixture/primary.md?digest=x",
    "fixture/%2e%2e.md", "fixture\\primary.md", "fixture/primary\x00.md", "fixture/é.md", "fixture/" + "a" * 256 + ".md"])
def test_hostile_editor_paths_are_rejected_before_http(remote_prompts, capsys, path):
    for arguments in (("read", path), ("write", path, remote_prompts["head"].digest), ("create", path)):
        code, result = invoke(capsys, *arguments, content="new")
        assert code == 2 and not result["ok"]
    assert remote_prompts["requests"] == []


@pytest.mark.parametrize("arguments", [
    ("clone", "https://example.invalid/prompts.git"), ("commit", "-m", "attempt"), ("push",),
    ("pull",), ("resolve", "fixture/primary.md"), ("publish", "https://example.invalid", "/not-a-token"),
])
def test_configured_git_authoring_never_touches_worktree(remote_prompts, capsys, monkeypatch, arguments):
    monkeypatch.setattr(prompts, "_run", lambda *args, **kwargs: pytest.fail("local Git must not run"))
    code, result = invoke(capsys, *arguments)
    assert code == 2 and result["code"] == "server_managed"
    assert "saves are already server-versioned" in result["error"]
    assert remote_prompts["requests"] == []
    assert remote_prompts["source"].read_text() == "precious unpublished local work\n"


def test_token_save_cannot_bootstrap_cookie_or_gain_write(remote_prompts, capsys, tmp_path):
    connection = remote_prompts["connection"].model_copy(update={"auth_mode": "token", "token_file": tmp_path / "unread-token"})
    connection.save()
    code, result = invoke(capsys, "write", "fixture/primary.md", remote_prompts["head"].digest, content="draft")
    assert code == 2 and not result["ok"]
    assert remote_prompts["requests"] == []
    assert not connection.session_path().exists()


@pytest.mark.parametrize("arguments", [("read", "fixture/primary.md"), ("status",)])
def test_token_current_head_and_status_reads_never_bootstrap_cookies(remote_prompts, capsys, tmp_path, arguments):
    token = tmp_path / "synthetic-token"
    token.write_text(remote_prompts["token"])
    token.chmod(0o600)
    connection = remote_prompts["connection"].model_copy(update={"auth_mode": "token", "token_file": token})
    connection.save()
    code, result = invoke(capsys, *arguments)
    assert code == 0 and result["ok"]
    assert len(remote_prompts["requests"]) == 1
    assert not connection.session_path().exists()


@pytest.mark.parametrize("arguments", [("catalog",), ("read", "fixture/primary.md"),
                                      ("write", "fixture/primary.md", "0" * 64), ("commit", "-m", "attempt")])
def test_invalid_configuration_never_activates_local_mode(remote_prompts, capsys, arguments):
    CatalogConnection.path().write_text('{"auth_mode":"preview"}')
    code, result = invoke(capsys, *arguments, content="draft")
    assert code == 2 and not result["ok"]
    assert remote_prompts["source"].read_text() == "precious unpublished local work\n"
    assert remote_prompts["requests"] == []


def test_sync_status_and_revision_commands_are_server_state(remote_prompts, capsys):
    assert invoke(capsys, "status")[1]["sync"]["server_head"] == remote_prompts["head"].source_commit
    for command in ("diff", "log"):
        code, result = invoke(capsys, command)
        assert code == 0 and result["source"] == "server"
        assert result["history"] == "current heads only"
        assert result["revisions"][0]["revision"] == str(remote_prompts["head"].revision)


@pytest.mark.parametrize("command", ["compile", "install", "sync"])
def test_configured_projection_routes_only_to_root_helper(remote_prompts, capsys, monkeypatch, tmp_path, command):
    calls = []
    def project(*args):
        calls.append(args)
        return tmp_path / "projection"
    monkeypatch.setattr(prompts.prompt_projection, "compile_server_prompt_projection", project, raising=False)
    monkeypatch.setattr(prompts.prompt_projection, "install_server_prompt_projection", project, raising=False)
    monkeypatch.setattr(prompts, "_repository", lambda: pytest.fail("local source must never resolve"))
    with pytest.raises(SystemExit) as exit_status:
        prompts.prompts_app([command])
    assert exit_status.value.code == 0
    assert str(tmp_path / "projection") in capsys.readouterr().out
    assert calls[0][0] == remote_prompts["connection"]
    assert len(calls[0]) == (2 if command == "compile" else 5)


def test_generation_invalidated_during_read_cannot_release_content(remote_prompts, capsys):
    remote_prompts["invalidate"] = True
    code, result = invoke(capsys, "read", "fixture/primary.md")
    assert code == 2 and not result["ok"]
    assert "content" not in result


def test_bounded_input_and_response_validation(remote_prompts, capsys):
    code, result = invoke(capsys, "write", "fixture/primary.md", remote_prompts["head"].digest,
                          content="x" * (MAX_EDITOR_BYTES + 1))
    assert code == 2 and not result["ok"] and remote_prompts["requests"] == []
    for payload in (b"{broken", b"x" * ((16 << 20) + 1)):
        remote_prompts["response"] = payload
        code, result = invoke(capsys, "read", "fixture/primary.md")
        assert code == 2 and not result["ok"]


@pytest.mark.parametrize("defect", ["duplicate", "bad-digest", "wrong-key", "too-large"])
def test_malformed_catalog_or_revision_fails_closed(remote_prompts, capsys, defect):
    record = remote_prompts["head"].model_dump(mode="json")
    command = ("read", "fixture/primary.md")
    if defect == "duplicate":
        remote_prompts["catalog"] = [record, record]
        command = ("catalog",)
    else:
        if defect == "bad-digest":
            record["digest"] = "0" * 64
        elif defect == "wrong-key":
            record["key"]["slug"] = "different"
        else:
            record["content"] = "x" * (MAX_EDITOR_BYTES + 1)
            record["digest"] = hashlib.sha256(record["content"].encode()).hexdigest()
        remote_prompts["response"] = json.dumps(record).encode()
    assert invoke(capsys, *command)[0] == 2


def test_create_uses_typed_request_and_shared_cookie_csrf_preserves_source(remote_prompts, capsys):
    source = remote_prompts["source"].read_bytes()
    content = "Unicode instructions: é\nExact trailing whitespace.  \n"
    code, result = invoke(capsys, "create", "instructions/new-profile.md", "--name", "New profile", content=content)
    assert code == 0 and result["ok"] is True
    assert result["path"] == "instructions/new-profile.md"
    assert result["digest"] == hashlib.sha256(content.encode()).hexdigest()
    saved = remote_prompts["head"]
    assert saved.content == content and saved.name == "New profile"
    assert result["revision"] == str(saved.revision)
    posts = [request for request in remote_prompts["requests"] if request.method == "POST" and request.url.path.endswith("/prompts")]
    assert len(posts) == 1
    assert PromptCreateRequest.model_validate_json(posts[0].content).key == saved.key
    assert not any(request.method == "PUT" for request in remote_prompts["requests"])
    assert remote_prompts["source"].read_bytes() == source
    assert invoke(capsys, "read", "instructions/new-profile.md")[1]["content"] == content


def test_create_defaults_display_name_to_slug(remote_prompts):
    saved = ServerPrompts(connection=remote_prompts["connection"]).create("instructions/example.md", "exact content")
    assert saved.name == "example" and saved.content == "exact content"


@pytest.mark.parametrize("failure", ["race", 401, 403, "post-forbidden", "offline"])
def test_create_failure_preserves_input_and_never_falls_back_local(remote_prompts, capsys, failure):
    state = remote_prompts
    state["failure"] = failure
    state["race"] = failure == "race"
    original = state["head"]
    code, result = invoke(capsys, "create", "instructions/new.md", content="keep caller buffer")
    assert code == 2 and result["ok"] is False
    assert "preserve" in result["error"].lower()
    assert result["code"] == ("conflict" if failure == "race" else "invalid")
    assert state["head"] == original
    assert state["source"].read_text() == "precious unpublished local work\n"
    assert not (state["source"].parents[1] / "instructions" / "new.md").exists()
    if failure in {401, 403, "post-forbidden"}:
        assert not state["connection"].session_path().exists()


def test_create_existing_key_conflicts_without_overwrite(remote_prompts, capsys):
    original = remote_prompts["head"]
    code, result = invoke(capsys, "create", "fixture/primary.md", content="do not overwrite")
    assert code == 2 and result["code"] == "conflict"
    assert remote_prompts["head"] == original


def test_create_rejects_token_before_reading_token_file(remote_prompts, capsys, tmp_path):
    connection = remote_prompts["connection"].model_copy(update={"auth_mode": "token", "token_file": tmp_path / "must-not-read-token"})
    connection.save()
    code, result = invoke(capsys, "create", "instructions/new.md", content="caller buffer")
    assert code == 2 and "read-only" in result["error"]
    assert remote_prompts["requests"] == []


def test_create_requires_server_without_local_repository_fallback(remote_prompts, capsys, monkeypatch):
    CatalogConnection.path().unlink()
    monkeypatch.setattr(prompts, "_repository", lambda: pytest.fail("must not resolve local source"))
    code, result = invoke(capsys, "create", "instructions/new.md", content="caller buffer")
    assert code == 2 and result["code"] == "server_required"
    assert "configured server" in result["error"]
    assert remote_prompts["requests"] == []


@pytest.mark.parametrize("content", ["", "x" * (MAX_EDITOR_BYTES + 1), "é" * ((MAX_EDITOR_BYTES // 2) + 1)])
def test_create_rejects_invalid_or_oversized_content_before_http(remote_prompts, capsys, content):
    code, result = invoke(capsys, "create", "instructions/new.md", content=content)
    assert code == 2 and not result["ok"]
    assert remote_prompts["requests"] == []


def test_create_invalid_utf8_is_rejected_before_http(remote_prompts, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"invalid-utf8-\xff")))
    with pytest.raises(SystemExit) as result:
        prompts.prompts_app(["create", "instructions/new.md"])
    assert result.value.code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False and "UTF-8" in output["error"]
    assert remote_prompts["requests"] == []


@pytest.mark.parametrize("name", ["", "x" * 121])
def test_create_invalid_name_is_rejected_before_http(remote_prompts, capsys, name):
    code, result = invoke(capsys, "create", "instructions/new.md", "--name", name, content="caller buffer")
    assert code == 2 and not result["ok"]
    assert remote_prompts["requests"] == []


@pytest.mark.parametrize("defect", ["json", "key", "digest", "content", "name", "oversized"])
def test_create_rejects_invalid_success_response(remote_prompts, capsys, defect):
    content = "caller buffer"
    record = remote_prompts["head"].model_dump(mode="json")
    record.update(key={"namespace": "instructions", "slug": "new"}, name="new", content=content,
                  digest=hashlib.sha256(content.encode()).hexdigest())
    if defect == "key":
        record["key"]["slug"] = "wrong"
    elif defect == "digest":
        record["digest"] = "0" * 64
    elif defect == "content":
        record.update(content="different", digest=hashlib.sha256(b"different").hexdigest())
    elif defect == "name":
        record["name"] = "different"
    remote_prompts["response"] = b"{broken" if defect == "json" else b"x" * ((16 << 20) + 1) if defect == "oversized" else json.dumps(record).encode()
    code, result = invoke(capsys, "create", "instructions/new.md", content=content)
    assert code == 2 and result["ok"] is False
    assert "preserve" in result["error"].lower()
