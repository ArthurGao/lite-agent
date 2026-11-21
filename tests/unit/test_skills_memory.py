"""Design §6 (stage S3a): skills are an index in the system prompt and a tool that loads one on
demand; memory is two tools and a sentence saying what it is for. Everything the model sees of
them is in the log: the assembled system prompt in `run_started`, the rest as tool results."""

from pathlib import Path
from typing import Any

import pytest
from lite_model import Message, ToolCall, ToolResult, ToolResults
from lite_model.testing import AsyncFakeModel

from lite_agent import (
    Agent,
    Allow,
    Budget,
    Deny,
    NotesMemory,
    RunContext,
    SkillDir,
    SkillInfo,
)
from lite_agent.events import RunStarted
from tests.helpers import Recording, agent, call, events_of, results_of, say

DEPLOY = """---
name: deploy
description: Ship a service to the demo VM, with the checks to run before and after.
---

# Deploy

1. Run the smoke test.
2. Push the image.
"""
REVIEW = """---
name: review
description: Review a diff for bugs before it is committed.
---

Read the diff twice.
"""


def skills(root: Path, **files: str) -> SkillDir:
    for name, text in files.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "SKILL.md").write_text(text)
    return SkillDir(root)


@pytest.fixture
def two(tmp_path: Path) -> SkillDir:
    return skills(tmp_path, review=REVIEW, deploy=DEPLOY)


# --- SkillDir ---


def test_a_skill_dir_indexes_name_and_description_sorted_by_name(two: SkillDir) -> None:
    assert two.index() == (
        SkillInfo(
            "deploy", "Ship a service to the demo VM, with the checks to run before and after."
        ),
        SkillInfo("review", "Review a diff for bugs before it is committed."),
    )


def test_loading_a_skill_gives_its_body_without_the_frontmatter(two: SkillDir) -> None:
    assert two.load("deploy") == "# Deploy\n\n1. Run the smoke test.\n2. Push the image."
    with pytest.raises(KeyError):
        two.load("nope")


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("# No frontmatter\n", "no frontmatter block"),
        ("---\nname: deploy\n---\nBody\n", "description"),
        ("---\ndescription: Something.\n---\nBody\n", "name"),
        ("---\nname: other\ndescription: Something.\n---\nBody\n", "does not match"),
        (
            "---\nname: deploy\ndescription: Something.\nBody with no closing line\n",
            "no frontmatter block",
        ),
        ("---\nname: deploy\ndescription:\n---\nBody\n", "description"),
        ("Intro\n---\nname: deploy\ndescription: Something.\n---\nBody\n", "no frontmatter block"),
    ],
    ids=[
        "none",
        "no_description",
        "no_name",
        "wrong_name",
        "unclosed",
        "empty_description",
        "late",
    ],
)
def test_a_malformed_skill_is_refused_when_the_directory_is_read(
    tmp_path: Path, text: str, problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        skills(tmp_path, deploy=text)


def test_a_directory_without_a_skill_file_is_not_a_skill(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "README.md").write_text("not a skill")
    assert skills(tmp_path, deploy=DEPLOY).index() == (
        SkillInfo(
            "deploy", "Ship a service to the demo VM, with the checks to run before and after."
        ),
    )


# --- skills in a run ---


async def test_the_skill_index_is_in_the_system_prompt_as_logged(two: SkillDir) -> None:
    model = AsyncFakeModel(agent=[say("Hi.")])
    a = agent(model, system="Be brief.", skills=two)

    await a.run("Hello.")

    started = (await events_of(a, "id1"))[1]
    assert isinstance(started, RunStarted)
    assert started.system == a.system_prompt()
    assert started.system.startswith("Be brief.\n\n")
    assert (
        "- deploy: Ship a service to the demo VM, with the checks to run before and after."
        in started.system
    )
    assert "- review: Review a diff for bugs before it is committed." in started.system
    assert "load_skill" in started.system
    assert "Run the smoke test" not in started.system  # the body is loaded on demand
    assert model.turn_calls[0].messages[0] == Message("system", started.system)  # what was sent
    assert [t.name for t in model.turn_calls[0].tools] == ["load_skill"]


async def test_the_assembled_prompt_is_the_same_every_time(two: SkillDir, tmp_path: Path) -> None:
    a = agent(AsyncFakeModel(), system="Be brief.", skills=two)
    again = agent(AsyncFakeModel(), system="Be brief.", skills=SkillDir(tmp_path))
    assert a.system_prompt() == a.system_prompt() == again.system_prompt()


async def test_load_skill_returns_the_body_as_a_tool_result(two: SkillDir) -> None:
    model = AsyncFakeModel(
        agent=[call(("c1", "load_skill", {"name": "deploy"})), say("Deploying.")]
    )
    a = agent(model, skills=two)

    await a.run("Ship it.")

    (loaded,) = results_of(await events_of(a, "id1"))
    assert (loaded.content, loaded.is_error) == (two.load("deploy"), False)
    assert model.turn_calls[1].messages[-1] == ToolResults((ToolResult("c1", two.load("deploy")),))


async def test_loading_an_unknown_skill_is_a_tool_error_that_names_the_real_ones(
    two: SkillDir,
) -> None:
    model = AsyncFakeModel(agent=[call(("c1", "load_skill", {"name": "nope"})), say("Ok.")])
    a = agent(model, skills=two)
    await a.run("Ship it.")
    (failed,) = results_of(await events_of(a, "id1"))
    assert failed.is_error and "nope" in failed.content
    assert "deploy" in failed.content and "review" in failed.content


def test_an_index_over_its_budget_is_refused_when_the_agent_is_built(two: SkillDir) -> None:
    with pytest.raises(ValueError, match="skills index"):
        Agent(
            role="agent", model=AsyncFakeModel(), skills=two, budget=Budget(skills_index_chars=50)
        )


def test_a_host_tool_may_not_take_a_built_in_name(two: SkillDir) -> None:
    with pytest.raises(ValueError, match="unique: load_skill"):
        agent(AsyncFakeModel(), Recording().tool("load_skill"), skills=two)
    with pytest.raises(ValueError, match="unique: remember"):
        agent(AsyncFakeModel(), Recording().tool("remember"), memory=NotesMemory())


# --- memory ---


async def test_remember_and_recall_go_through_the_memory_and_the_log() -> None:
    memory = NotesMemory()
    model = AsyncFakeModel(
        agent=[
            call(("c1", "remember", {"note": "The demo VM is cairn-demo."})),
            call(("c2", "recall", {"query": "demo vm"})),
            say("It is cairn-demo."),
        ]
    )
    a = agent(model, memory=memory)

    await a.run("Which VM?")

    assert memory.notes == ["The demo VM is cairn-demo."]
    saved, found = results_of(await events_of(a, "id1"))
    assert saved.is_error is False
    assert found.content == "- The demo VM is cairn-demo."
    assert [t.name for t in model.turn_calls[0].tools] == ["recall", "remember"]


async def test_recall_says_so_when_nothing_matches() -> None:
    model = AsyncFakeModel(agent=[call(("c1", "recall", {"query": "anything"})), say("Nothing.")])
    a = agent(model, memory=NotesMemory())
    await a.run("What do you know?")
    (found,) = results_of(await events_of(a, "id1"))
    assert found.content == "Nothing is noted about that." and found.is_error is False


async def test_the_memory_prompt_says_what_memory_is_for_and_scripts_nothing() -> None:
    a = agent(AsyncFakeModel(), system="Be brief.", memory=NotesMemory())
    prompt = a.system_prompt()
    assert prompt.startswith("Be brief.\n\n")
    assert "recall" in prompt and "remember" in prompt
    for scripted in ("always", "never", "must", "only when"):  # KB §13.1: no write rules
        assert scripted not in prompt.lower()


async def test_a_gate_can_refuse_remember_like_any_other_tool() -> None:
    class NoWrites:
        name = "no-writes"

        async def check(self, c: ToolCall, ctx: RunContext) -> Any:
            return Deny("memory is read-only here") if c.name == "remember" else Allow()

    memory = NotesMemory()
    model = AsyncFakeModel(agent=[call(("c1", "remember", {"note": "x"})), say("Ok.")])
    a = agent(model, memory=memory, gates=[NoWrites()])
    await a.run("Note it.")
    (denied,) = results_of(await events_of(a, "id1"))
    assert denied.error_kind == "denied" and memory.notes == []


async def test_remember_refuses_an_empty_note_before_it_runs() -> None:
    memory = NotesMemory()
    model = AsyncFakeModel(agent=[call(("c1", "remember", {"note": ""})), say("Ok.")])
    a = agent(model, memory=memory)
    await a.run("Note it.")
    (refused,) = results_of(await events_of(a, "id1"))
    assert (refused.error_kind, refused.executed) == ("invalid_args", False) and memory.notes == []


# --- nothing changes without them ---


async def test_without_skills_or_memory_the_prompt_and_tools_are_the_hosts_own() -> None:
    tools = Recording()
    model = AsyncFakeModel(agent=[say("Hi.")])
    a = agent(model, tools.tool("get_time"), system="Be brief.")
    await a.run("Hello.")
    assert a.system_prompt() == "Be brief."
    assert [t.name for t in model.turn_calls[0].tools] == ["get_time"]
    assert agent(AsyncFakeModel()).system_prompt() == ""


def test_a_prompt_without_a_host_part_starts_with_the_first_part_there_is(two: SkillDir) -> None:
    assert agent(AsyncFakeModel(), skills=two).system_prompt().startswith("Skills are")
    assert (
        agent(AsyncFakeModel(), memory=NotesMemory())
        .system_prompt()
        .startswith("You have a memory")
    )


async def test_skills_and_memory_together_keep_a_stable_order(two: SkillDir) -> None:
    model = AsyncFakeModel(agent=[say("Hi.")])
    a = agent(model, Recording().tool("get_time"), skills=two, memory=NotesMemory())
    await a.run("Hello.")
    assert [t.name for t in model.turn_calls[0].tools] == [
        "get_time",
        "load_skill",
        "recall",
        "remember",
    ]
    prompt = a.system_prompt()
    assert prompt.index("load_skill") < prompt.index("recall")


# --- simplify review 2026-11-21 ---


def test_an_agent_with_built_ins_can_be_copied_and_keeps_the_hosts_tools_apart(
    two: SkillDir,
) -> None:
    import dataclasses

    host = Recording().tool("get_time")
    a = agent(AsyncFakeModel(), host, skills=two, memory=NotesMemory())
    assert a.tools == (host,)  # what the host gave, untouched
    assert [t.name for t in a.all_tools] == ["get_time", "load_skill", "recall", "remember"]

    copy = dataclasses.replace(a, budget=Budget(turns=5))  # used to fail: built-ins added twice

    assert [t.name for t in copy.all_tools] == ["get_time", "load_skill", "recall", "remember"]
    assert copy.tool("load_skill") is not None and copy.tool("nope") is None


class Growing:
    """A skill source whose index grows after the agent is built."""

    def __init__(self) -> None:
        self.items = [SkillInfo("deploy", "Ship it.")]

    def index(self) -> list[SkillInfo]:
        return list(self.items)

    def load(self, name: str) -> str:
        return "body"


async def test_the_prompt_is_fixed_when_the_agent_is_built() -> None:
    # the budget is checked on the index the agent was built with, so that is the one it sends
    source = Growing()
    model = AsyncFakeModel(agent=[say("Hi.")])
    a = agent(model, skills=source, budget=Budget(skills_index_chars=40))
    built = a.system_prompt()

    source.items += [SkillInfo(f"skill-{i}", "x" * 80) for i in range(20)]
    await a.run("Hello.")

    assert a.system_prompt() == built
    started = (await events_of(a, "id1"))[1]
    assert isinstance(started, RunStarted) and started.system == built
    assert "skill-3" not in started.system


# --- Codex review 2026-11-21 ---


def test_a_repeated_frontmatter_key_is_refused(tmp_path: Path) -> None:
    twice = "---\nname: deploy\ndescription: One.\ndescription: Two.\n---\nBody\n"
    with pytest.raises(ValueError, match="more than once"):
        skills(tmp_path, deploy=twice)


def test_a_skill_file_saved_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "SKILL.md").write_bytes(b"\xef\xbb\xbf" + DEPLOY.encode())
    assert SkillDir(tmp_path).index()[0].name == "deploy"


def test_the_index_is_sorted_by_name_whatever_order_a_source_gives() -> None:
    class Unordered:
        def index(self) -> list[SkillInfo]:
            return [SkillInfo("zeta", "Last."), SkillInfo("alpha", "First.")]

        def load(self, name: str) -> str:
            return "body"

    prompt = agent(AsyncFakeModel(), skills=Unordered()).system_prompt()
    assert prompt.index("- alpha: First.") < prompt.index("- zeta: Last.")


async def test_notes_are_found_whatever_punctuation_surrounds_a_word() -> None:
    memory = NotesMemory()
    await memory.remember("Deploys go to cairn-demo, on Fridays!")
    assert await memory.recall("Fridays?") == "- Deploys go to cairn-demo, on Fridays!"
    assert await memory.recall("DEMO") == "- Deploys go to cairn-demo, on Fridays!"
    assert await memory.recall("mondays") == "Nothing is noted about that."


async def test_remember_refuses_a_note_of_only_whitespace() -> None:
    memory = NotesMemory()
    model = AsyncFakeModel(agent=[call(("c1", "remember", {"note": "  \n "})), say("Ok.")])
    a = agent(model, memory=memory)
    await a.run("Note it.")
    (refused,) = results_of(await events_of(a, "id1"))
    assert (refused.error_kind, refused.executed) == ("invalid_args", False) and memory.notes == []


async def test_an_interrupted_remember_waits_to_be_checked_and_is_not_repeated() -> None:
    from tests.helpers import FailingOnce

    memory = NotesMemory()
    model = AsyncFakeModel(agent=[call(("c1", "remember", {"note": "x"})), say("Ok.")])
    a = agent(model, memory=memory, sessions=FailingOnce(6))  # the note was kept, its result lost
    broken = await a.run("Note it.")
    waiting = await a.resume(broken.session_id)
    assert waiting.pause is not None and waiting.pause.waiting[0].reason == "reconcile"
    assert memory.notes == ["x"]  # once
