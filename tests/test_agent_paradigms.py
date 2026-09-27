"""A paradigm: content authored once, projected per-agent as a skill OR a system-prompt fragment.
The same authored unit is a lazily-loaded skill for one agent and baked into the always-present
system prompt for another — the choice belongs to the policy, not to the paradigm file.
"""

import json

import pytest

from interact.agents.paradigms import ParadigmError, plan_projections, read_paradigm
from interact.agents.policy import Policy, PolicyError


@pytest.fixture
def paradigm_repository(tmp_path):
    """One paradigm file, frontmatter + body — the minimal repository the reader needs."""
    root = tmp_path / "prompts"
    (root / "paradigms").mkdir(parents=True)
    (root / "paradigms" / "coding.md").write_text(
        '---\nname: coding\ndescription: "How code gets written here"\n---\n'
        "Generic to the max, unlocked by typed config. Test-driven, edge-case-first.\n"
    )
    return root


def test_a_paradigm_file_is_read_as_name_description_and_body(paradigm_repository):
    p = read_paradigm("coding", paradigm_repository)
    assert p.name == "coding"
    assert p.description == "How code gets written here"
    assert "Generic to the max" in p.body


def test_an_unknown_paradigm_is_refused_where_it_is_asked_for(paradigm_repository):
    with pytest.raises(ParadigmError, match="coding-typo"):
        read_paradigm("coding-typo", paradigm_repository)


def test_the_policy_holds_which_agent_uses_which_paradigm_and_how(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "paradigms": {
            "tester": [{"paradigm": "coding", "as": "system_prompt"}],
            "librarian": [{"paradigm": "coding", "as": "skill"}],
        },
    }))
    policy = Policy.load(path)
    assert policy.paradigms["tester"][0].paradigm == "coding"
    assert policy.paradigms["tester"][0].projection == "system_prompt"
    assert policy.paradigms["librarian"][0].projection == "skill"


def test_an_unknown_projection_kind_is_refused_at_load_not_at_spawn(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "paradigms": {"tester": [{"paradigm": "coding", "as": "sticky-note"}]},
    }))
    with pytest.raises(PolicyError, match="sticky-note"):
        Policy.load(path)


def test_assigning_writes_back_the_one_file_every_front_end_reads(tmp_path):
    """Same CAS read-modify-write shape as `set_provider_active` — one fact, one file, so the
    panel's choice and the spawn's choice can never drift apart."""
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"profiles": {"eyes": "cap.vlm"}}))
    policy = Policy.load(path)

    policy.assign_paradigm("tester", "coding", "system_prompt", path=path)

    on_disk = json.loads(path.read_text())
    assert on_disk["paradigms"]["tester"] == [{"paradigm": "coding", "as": "system_prompt"}]
    assert on_disk["profiles"] == {"eyes": "cap.vlm"}, "an unrelated fact must survive the write"
    assert policy.paradigms["tester"][0].paradigm == "coding", "the in-memory copy agrees too"

    # Assigning the SAME agent a SECOND paradigm appends rather than clobbering the first.
    policy.assign_paradigm("tester", "caveman-output", "skill", path=path)
    on_disk = json.loads(path.read_text())
    assert [a["paradigm"] for a in on_disk["paradigms"]["tester"]] == ["coding", "caveman-output"]

    # Re-assigning the SAME paradigm to the SAME agent replaces its projection, never duplicates.
    policy.assign_paradigm("tester", "coding", "skill", path=path)
    on_disk = json.loads(path.read_text())
    assert on_disk["paradigms"]["tester"] == [
        {"paradigm": "coding", "as": "skill"}, {"paradigm": "caveman-output", "as": "skill"},
    ]


def test_the_same_paradigm_is_a_skill_for_one_agent_and_baked_in_for_another(paradigm_repository, tmp_path):
    """The whole point: one authored unit, a per-agent choice of projection — never a global flag
    on the paradigm that binds every agent to the same answer."""
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "paradigms": {
            "tester": [{"paradigm": "coding", "as": "system_prompt"}],
            "librarian": [{"paradigm": "coding", "as": "skill"}],
        },
    }))
    policy = Policy.load(path)

    plan = plan_projections(policy, paradigm_repository)

    assert plan.system_prompt_fragments["tester"] == ["Generic to the max, unlocked by typed config. Test-driven, edge-case-first."]
    assert "tester" not in [s.agent for s in plan.skills]
    assert plan.skills == [type(plan.skills[0])(
        agent="librarian", name="coding", description="How code gets written here",
        body="Generic to the max, unlocked by typed config. Test-driven, edge-case-first.",
    )]
    assert "librarian" not in plan.system_prompt_fragments


def test_a_paradigm_named_in_policy_but_missing_from_the_repository_is_named_not_silently_dropped(tmp_path):
    """A skipped agent instruction is a 3am surprise, not a warning worth losing in a log."""
    root = tmp_path / "prompts"
    (root / "paradigms").mkdir(parents=True)
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "paradigms": {"tester": [{"paradigm": "does-not-exist", "as": "skill"}]},
    }))
    policy = Policy.load(path)
    with pytest.raises(ParadigmError, match="does-not-exist"):
        plan_projections(policy, root)
