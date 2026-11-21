"""The S5 lite-eval target (design §15): the dataset names exactly the task set, the suites
load, and on lite-model's fake every task is done with the same metrics on every run (the
deterministic run in CI). One opt-in test runs a task on the real local Ollama."""

import dataclasses
from pathlib import Path

import pytest
from lite_eval.datasets.local import read_cases
from lite_eval.suite import load_suite

from lite_agent.eval_target import AgentTarget, agent
from lite_agent.evals import TASKS
from lite_agent.evals.tasks import Metrics

EVAL = Path(__file__).resolve().parents[2] / "eval" / "lite-eval"
FIELDS = [f.name for f in dataclasses.fields(Metrics)]


def test_the_dataset_names_exactly_the_task_set() -> None:
    cases = read_cases(EVAL / "datasets" / "agent")
    assert list(cases) == [t.id for t in TASKS]
    for t in TASKS:
        case = cases[t.id]
        assert (case.input, case.expected) == ({"task": t.id}, {"done": True})
        assert case.tags == {"lang": t.lang, "kind": t.kind}


@pytest.mark.parametrize("suite", ["agent-fake.yaml", "agent-ollama.yaml"])
def test_the_suites_load(suite: str) -> None:
    loaded = load_suite(EVAL / "suites" / suite)
    assert loaded is not None


async def outputs(target: AgentTarget) -> dict[str, dict[str, object]]:
    cases = read_cases(EVAL / "datasets" / "agent")
    await target.setup(None)  # type: ignore[arg-type]  # the fake reads nothing from it
    try:
        got: dict[str, dict[str, object]] = {}
        for case in cases.values():
            out = await target.run(case)
            assert isinstance(out.value, dict)
            got[case.id] = {k: v for k, v in out.value.items() if k != "seconds"}
            assert [s.name for s in out.trace.spans][:2] == ["session_started", "run_started"]
        return got
    finally:
        await target.teardown()


async def test_on_the_fake_every_task_is_done_the_same_way_every_time() -> None:
    first, second = await outputs(agent()), await outputs(agent())
    assert first == second
    assert all(v["done"] is True and v["why"] == "done" for v in first.values())
    assert sorted(first["long-en"]) == sorted(f for f in FIELDS if f != "seconds")
    assert (first["long-en"]["compacted"], first["done-en"]["auto_continues"]) == (True, 1)


def test_the_target_says_what_decides_its_output() -> None:
    fake, live = agent(), agent("ollama", "qwen3:8b")
    assert (fake.deterministic, live.deterministic) == (True, False)
    assert live.config() == {"backend": "ollama", "model": "qwen3:8b", "reasoning_effort": "none"}
    assert set(fake.versions()) == {"lite-agent", "lite-model", "code"}
    assert fake.billing() == "free"


@pytest.mark.ollama
async def test_a_tool_task_runs_on_the_local_ollama() -> None:
    # the smoke (design §15 S5): the run ends and every metric is there; whether the model
    # gets it right is the baseline's business, not this test's
    target = agent("ollama", "qwen3:8b")
    case = read_cases(EVAL / "datasets" / "agent")["chain-en"]
    await target.setup(None)  # type: ignore[arg-type]
    out = await target.run(case)
    assert isinstance(out.value, dict) and sorted(out.value) == sorted(FIELDS)
    m = Metrics(**out.value)  # type: ignore[arg-type]
    assert m.outcome in ("completed", "failed") and m.turns >= 1 and m.tool_calls >= 1


def test_the_code_version_moves_when_any_of_lite_agents_code_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    import lite_agent
    from lite_agent.eval_target import code_version

    copy = tmp_path / "lite_agent"
    shutil.copytree(
        Path(lite_agent.__file__).parent, copy, ignore=shutil.ignore_patterns("__pycache__")
    )
    monkeypatch.setattr(lite_agent, "__file__", str(copy / "__init__.py"))
    before = code_version()
    assert code_version() == before
    (copy / "notes.txt").write_text("not code\n")
    assert code_version() == before
    for changed in ("loop.py", "evals/skills/refund-policy/SKILL.md"):
        path = copy / changed
        path.write_text(path.read_text() + "\n")
        assert code_version() != before, changed
        before = code_version()
