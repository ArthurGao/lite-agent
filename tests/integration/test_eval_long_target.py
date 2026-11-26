"""The long tasks' lite-eval target (design §15, masking on long runs): the dataset names exactly
the long set, the suites load, and on lite-model's fake each config does what it is there to
compare: compaction alone writes a summary on every task; masking first masks old reports and no
summary is needed. The S5 set and its tests are untouched."""

from pathlib import Path

import pytest
from lite_eval.datasets.local import read_cases
from lite_eval.suite import load_suite

from lite_agent.eval_target import AgentTarget, agent
from lite_agent.evals.archive import LONG_TASKS

EVAL = Path(__file__).resolve().parents[2] / "eval" / "lite-eval"


def test_the_dataset_names_exactly_the_long_tasks() -> None:
    cases = read_cases(EVAL / "datasets" / "agent-long")
    assert list(cases) == [t.id for t in LONG_TASKS]
    for t in LONG_TASKS:
        case = cases[t.id]
        assert (case.input, case.expected) == ({"task": t.id}, {"done": True})
        assert case.tags == {"lang": t.lang, "kind": t.kind}


@pytest.mark.parametrize("suite", ["agent-long-fake.yaml", "agent-long-ollama.yaml"])
def test_the_suites_load_and_compare_masking_off_and_on(suite: str) -> None:
    loaded = load_suite(EVAL / "suites" / suite)
    (target,) = loaded.spec.targets
    masking = [(c.get("mask_at"), c.get("mask_keep")) for c in target.configs]
    assert masking == [(None, None), (2000, 2)]  # the thresholds measured on the fake


async def outputs(target: AgentTarget) -> dict[str, dict[str, object]]:
    cases = read_cases(EVAL / "datasets" / "agent-long")
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


async def test_on_the_fake_compaction_alone_summarises_every_task() -> None:
    first, second = await outputs(agent()), await outputs(agent())
    assert first == second
    for got in first.values():
        assert (got["done"], got["why"]) == (True, "done")
        assert (got["compacted"], got["masked"], got["rereads"]) == (True, 0, 0)


async def test_on_the_fake_masking_first_leaves_nothing_to_summarise() -> None:
    first = await outputs(agent(mask_at=2000, mask_keep=2))
    assert first == await outputs(agent(mask_at=2000, mask_keep=2))
    for got in first.values():
        assert (got["done"], got["why"]) == (True, "done")
        assert (got["compacted"], got["masked"], got["rereads"]) == (False, 9, 0)
