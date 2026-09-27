"""Local function registry: a `@interact.function`-decorated Python callable, or a registered
shell command, invocable from a workflow's "On my PC" machine-function node."""

import json
import os
from pathlib import Path

import pytest

import interact
from interact.functions import FunctionRegistry, discover_python, discover_shell, invoke


def test_bare_decorator_marks_a_typed_function_with_its_docstring(tmp_path: Path) -> None:
    source = tmp_path / "greeter.py"
    source.write_text(
        "import interact\n\n"
        "@interact.function\n"
        "def greet(name: str) -> str:\n"
        "    \"\"\"Greets someone by name.\"\"\"\n"
        "    return f'hello {name}'\n"
    )
    entries = discover_python(source)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "greet" and entry.kind == "python" and entry.permission == "read_only"
    assert entry.description == "Greets someone by name."
    assert {(port.name, port.direction, port.value_type) for port in entry.ports} == {
        ("name", "input", "text"), ("result", "output", "text"),
    }
    assert invoke(entry, {"name": "alan"}, working_directory=tmp_path) == "hello alan"


def test_parameterized_decorator_types_every_port_from_hints_and_defaults(tmp_path: Path) -> None:
    source = tmp_path / "counter.py"
    source.write_text(
        "import interact\n\n"
        "@interact.function(description='Adds two numbers.', permission='full_access')\n"
        "def add(a: int, b: int = 1) -> int:\n"
        "    return a + b\n"
    )
    entry = discover_python(source)[0]
    assert entry.permission == "full_access"
    ports = {port.name: port for port in entry.ports}
    assert ports["a"].required is True and ports["b"].required is False
    assert ports["a"].value_type == "number" and ports["result"].value_type == "number"
    assert invoke(entry, {"a": 2, "b": 3}, working_directory=tmp_path) == 5


def test_untyped_parameter_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.py"
    source.write_text("import interact\n\n@interact.function\ndef broken(x) -> str:\n    return str(x)\n")
    with pytest.raises(ValueError, match="type hint"):
        discover_python(source)


def test_shell_function_types_every_placeholder_as_text_and_runs_argv_safely(tmp_path: Path) -> None:
    entry = discover_shell("hostname_like", "Echoes an argument.", ("echo", "-n", "{value}"))
    assert {port.name for port in entry.ports} == {"value", "result"}
    assert entry.permission == "full_access"
    assert invoke(entry, {"value": "hi; rm -rf /"}, working_directory=tmp_path) == "hi; rm -rf /"


def test_shell_function_argument_never_reaches_a_shell(tmp_path: Path) -> None:
    """No `shell=True`: an argument containing shell metacharacters is passed as ONE argv token
    to `echo`, never interpreted — the classic injection this design structurally cannot have."""
    entry = discover_shell("echoer", "Echoes.", ("echo", "-n", "{value}"))
    assert invoke(entry, {"value": "$(whoami)"}, working_directory=tmp_path) == "$(whoami)"


def test_registry_round_trips_as_a_owner_only_file(tmp_path: Path) -> None:
    registry = FunctionRegistry(tmp_path / "config" / "functions.json")
    entry = discover_shell("pwd_like", "Prints the working directory.", ("pwd",))
    registry.add(entry)
    assert [item.name for item in registry.load()] == ["pwd_like"]
    assert os.stat(registry.config_path).st_mode & 0o777 == 0o600
    assert registry.remove("pwd_like") is True
    assert registry.load() == ()
    assert registry.remove("pwd_like") is False


def test_registered_python_function_survives_its_original_file_disappearing(tmp_path: Path) -> None:
    """A function registered from a scratch/temp script must keep working after that file is
    gone (temp cleanup, a reboot clearing /tmp) -- `add()` copies the source into the registry's
    own storage rather than remembering an external path that can vanish."""
    source_dir = tmp_path / "scratch"
    source_dir.mkdir()
    source = source_dir / "greeter.py"
    source.write_text("import interact\n\n@interact.function\ndef greet(name: str) -> str:\n    \"\"\"Greets.\"\"\"\n    return f'hi {name}'\n")
    registry = FunctionRegistry(tmp_path / "config" / "functions.json")
    entry = discover_python(source)[0]
    stored = registry.add(entry)

    import shutil
    shutil.rmtree(source_dir)

    reloaded = registry.get("greet")
    assert Path(reloaded.path).is_file()
    assert not Path(entry.path).exists()
    assert invoke(reloaded, {"name": "alan"}, working_directory=tmp_path) == "hi alan"


def test_registry_get_raises_for_unknown_name(tmp_path: Path) -> None:
    registry = FunctionRegistry(tmp_path / "functions.json")
    with pytest.raises(KeyError):
        registry.get("nope")


def test_function_summary_carries_a_stable_content_version(tmp_path: Path) -> None:
    source = tmp_path / "fn.py"
    source.write_text("import interact\n\n@interact.function\ndef ping() -> str:\n    return 'pong'\n")
    entry = discover_python(source)[0]
    assert entry.version == entry.digest()
    summary = entry.summary()
    assert summary.name == "ping" and summary.version == entry.version


def test_cli_functions_add_python_list_and_test_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    source = tmp_path / "greeter.py"
    source.write_text("import interact\n\n@interact.function\ndef greet(name: str) -> str:\n    \"\"\"Greets.\"\"\"\n    return f'hi {name}'\n")
    from interact.cli.app import app as cli

    with pytest.raises(SystemExit) as result:
        cli(["functions", "add", str(source)])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "added": ["greet"]}

    with pytest.raises(SystemExit):
        cli(["functions", "list"])
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["name"] == "greet" and listed[0]["kind"] == "python"

    with pytest.raises(SystemExit) as result:
        cli(["functions", "test", "greet", "--arg", "name=alan"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": "hi alan"}


def test_cli_functions_add_shell_uses_end_of_options_delimiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from interact.cli.app import app as cli

    with pytest.raises(SystemExit) as result:
        cli(["functions", "add", "echoer", "--", "echo", "-n", "{value}"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "added": ["echoer"]}

    with pytest.raises(SystemExit) as result:
        cli(["functions", "test", "echoer", "--arg", "value=hi there"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": "hi there"}

    with pytest.raises(SystemExit) as result:
        cli(["functions", "remove", "echoer"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_invoke_rejects_arguments_outside_the_declared_ports(tmp_path: Path) -> None:
    entry = discover_shell("echoer", "Echoes.", ("echo", "-n", "{value}"))
    with pytest.raises(ValueError):
        invoke(entry, {"value": "ok", "extra": "nope"}, working_directory=tmp_path)
    with pytest.raises(ValueError):
        invoke(entry, {}, working_directory=tmp_path)
