"""A message is never refused for its length (owner 2026-10-21: « I got and error for exceeding characters... why are you
putting these shit limits everywhere ? »): a turn takes it inline up to `MessageTurn.limit`, and past that the agent is
handed the whole text as a file it reads, with its opening inline."""

import json
import os
import uuid

import pytest

from galaius.agents import messaging
from galaius.agents import agent_queue
from galaius.agents import registry as reg
from tests.support import register_run

COORDINATOR = str(uuid.UUID(int=7 << 96))


@pytest.fixture
def coordinator(monkeypatch) -> reg.AgentRun:
    monkeypatch.setattr(messaging, "policy_for_continuation", lambda run, provider, environment: (None, None, run.model, None))
    monkeypatch.setattr(messaging, "_dispatch", lambda *arguments, **keywords: None, raising=False)
    return register_run(COORDINATOR, agent="main", name="main", task="coordinate", pid=os.getpid(), provider_session_id=COORDINATOR)


def _sent(length: int) -> tuple[str, str]:
    text = ("Voici le plan complet, ligne par ligne. " * (length // 40 + 1))[:length]
    delivery = messaging.queue_message(COORDINATOR, text, sender=reg.PERSON_SENDER)
    assert delivery.state != "error", delivery.text
    [item] = [item for item in agent_queue.items(COORDINATOR) if item.state == "pending"]
    return text, item.turn.prompt(reg.get_run(COORDINATOR), item.message_id)


def test_a_50000_character_message_to_a_coordinator_is_queued_whole_and_handed_over_as_a_file(coordinator):
    text, prompt = _sent(50_000)
    assert reg.message_for(COORDINATOR, agent_queue.items(COORDINATOR)[-1].message_id).text == text   # the transcript keeps it whole
    assert text in prompt   # within what one turn takes in: inline, whole


def test_a_message_past_what_one_turn_takes_in_reaches_the_agent_as_a_file_it_reads(coordinator):
    text, prompt = _sent(agent_queue.INLINE_CHARS + 1_000)
    assert text not in prompt and text[:500] in prompt and f"{len(text):,} characters" in prompt
    filed = next(word for word in prompt.split() if word.endswith(".md"))
    assert open(filed, encoding="utf-8").read() == text


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_turn_too_long_for_a_command_line_reaches_its_cli_whole(coordinator, provider):
    """70,000 accented characters are 140 KB: past what one command-line argument carries (E2BIG on Linux). Claude reads
    the turn as its stdin message; Codex reads it on its stdin behind `-`."""
    from galaius.agents.providers import PROVIDERS, fits_argv
    from galaius.agents.run import carried

    text = "é" * 70_000
    cli = PROVIDERS[provider]
    argv, stdin = carried(cli, text, lambda said: ["cli", "--", said], lambda: ["cli", "--input-format", "stream-json"])
    assert all(fits_argv(argument) for argument in argv)
    if cli.starts_ahead:
        [said] = [line["message"]["content"] for line in map(json.loads, stdin.decode().splitlines()) if line.get("type") == "user"]
        assert argv == ["cli", "--input-format", "stream-json"] and said == text
    else:
        assert argv == ["cli", "--", "-"] and stdin.decode() == text


@pytest.mark.parametrize("turn", ["first", "resumed"])
def test_codex_reads_a_long_brief_on_its_stdin(turn):
    """Codex's own command lines, its first turn (the brief its role definition is baked into) and a resumed one: the
    text past one argument is read behind `-`, after `--`."""
    from galaius.agents.providers import CodexProvider, fits_argv

    codex, text = CodexProvider(), "é" * 70_000
    line = codex.command(text, cwd=".", model=None, mcp_config=None, run_id="run") if turn == "first" else codex.resume_command("session", text)
    argv, stdin = codex.on_stdin(line)
    assert argv[-1] == "-" and "--" in argv and all(fits_argv(argument) for argument in argv) and stdin.decode().endswith(text)


WINDOWS_LINE = 32_767


@pytest.mark.parametrize(("prompt_chars", "turn_chars", "turn"), [
    (49_750, 200, "first"),       # role main's definition, a short prompt (2026-11-09: every main start refused on Windows)
    (19_000, 15_900, "first"),    # a reviewer's definition, a turn just under what one argument carries
    (49_750, 70_000, "first"),    # both past their argument: the turn on stdin, the definition in its file
    (49_750, 200, "resumed"),     # every later turn resends the definition
])
def test_every_claude_command_line_fits_windows(monkeypatch, tmp_path, prompt_chars, turn_chars, turn):
    """Windows refuses a whole command line past 32,767 characters (WinError 206, « Nom de fichier ou extension trop
    long »): a role's definition reaches Claude from a file there, whatever its size; the CLI's line stays under it."""
    import subprocess
    import sys
    from pathlib import Path

    from galaius_core.workflows.contracts import WORKSPACE_NAME_MAX

    from galaius.agents import providers
    from galaius.agents.run import carried
    from galaius.host.paths import UserPaths

    monkeypatch.setattr(providers, "ARGV_TEXT_BYTES", 16_000)
    monkeypatch.setattr(providers, "DEFINITION_ARGV_BYTES", 0)
    monkeypatch.setenv(UserPaths.AGENTS_OVERRIDE, str(tmp_path))
    claude, run_id = providers.ClaudeCodeProvider(), str(uuid.uuid4())
    prompt, text = ("« Règle » \"citée\"\n" * prompt_chars)[:prompt_chars], "é" * turn_chars
    options = {"model": None, "mcp_config": None, "agent": "main", "agent_prompt": prompt, "allowed_tools": ["Bash"]}
    if turn == "first":
        argv, _ = carried(claude, text, lambda said: claude.command(said, cwd=".", run_id=run_id, **options),
                          lambda: claude.ahead_command(cwd=".", run_id=run_id, **options))
    else:
        argv, _ = carried(claude, text, lambda said: claude.resume_command(run_id, said, **options),
                          lambda: claude.resume_ahead_command(run_id, **options))
    assert len(subprocess.list2cmdline(argv)) < WINDOWS_LINE
    project = tmp_path / ".galaius" / "projects" / ("Aconit-" * 10)[:WORKSPACE_NAME_MAX]   # the longest project folder name
    project.mkdir(parents=True)
    subprocess.run([sys.executable, "-c", "pass", *argv[1:]], cwd=project, check=True, timeout=60)   # the OS starts it (Windows: CreateProcess)
    filed = Path(argv[argv.index("--agents") + 1])
    assert filed.parent == tmp_path / "definitions" and json.loads(filed.read_text(encoding="utf-8"))["main"]["prompt"] == prompt


def test_a_role_definition_in_a_file_still_tells_two_definitions_apart_on_the_command_line(monkeypatch, tmp_path):
    """A child started ahead is reused only for the same command line: a changed role prompt names another file."""
    from galaius.agents import providers
    from galaius.host.paths import UserPaths

    monkeypatch.setattr(providers, "DEFINITION_ARGV_BYTES", 0)
    monkeypatch.setenv(UserPaths.AGENTS_OVERRIDE, str(tmp_path))
    said = providers.ClaudeCodeProvider.agents_argument
    assert said({"main": {"prompt": "a"}}) == said({"main": {"prompt": "a"}}) != said({"main": {"prompt": "b"}})
