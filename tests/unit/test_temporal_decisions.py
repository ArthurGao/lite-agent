"""Which answers a workflow keeps between runs (design §9; Codex review 2026-11-21): answers
that arrived while a resume ran are kept only for the calls that resume left still waiting."""

from lite_agent.temporal.types import Call, Decision, Step, ToolResultData
from lite_agent.temporal.workflows import still_relevant


def paused(*ids: str) -> Step:
    return Step("s", "r", "paused", "paused", waiting=[Call(i, "approval", "pay") for i in ids])


def test_an_answer_for_a_call_that_still_waits_is_kept() -> None:
    late = Decision(approve={"c2": True})
    assert still_relevant(late, asked={"c1", "c2"}, step=paused("c2")) == Decision(
        approve={"c2": True}
    )


def test_an_answer_for_a_call_of_another_pause_is_dropped() -> None:
    # c1 was answered; the new turn's call happens to be named c1 again
    late = Decision(approve={"c1": True}, results={"c3": ToolResultData("x")})
    assert still_relevant(late, asked={"c2"}, step=paused("c1", "c3")) is None


def test_nothing_is_kept_when_the_run_did_not_pause() -> None:
    done = Step("s", "r", "completed", "end_turn")
    assert still_relevant(Decision(approve={"c1": True}), asked={"c1"}, step=done) is None
    assert still_relevant(None, asked={"c1"}, step=paused("c1")) is None
