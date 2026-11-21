"""The README's example is code that runs (design §15 S5.5): its Python block is executed as
written, with only lite-model's `build_async` swapped for a scripted fake, so a README that
drifts from the library fails here instead of in a new project."""

import re
from pathlib import Path
from typing import Any

import lite_model
import pytest
from lite_model.testing import AsyncFakeModel

from tests.helpers import call, say

README = Path(__file__).resolve().parents[2] / "README.md"


def example() -> str:
    text = README.read_text(encoding="utf-8")
    blocks: list[str] = re.findall(r"```python\n(.*?)```", text, re.S)
    assert len(blocks) == 1, "the README has one Python example"
    return blocks[0]


def test_the_readme_example_runs_as_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[tuple[Any, ...]] = []

    def build_async(*args: Any, **kwargs: Any) -> AsyncFakeModel:
        built.append(args)
        return AsyncFakeModel(
            agent=[call(("c1", "get_time", {"city": "Paris"})), say("It is 12:00 in Paris.")]
        )

    monkeypatch.setattr(lite_model, "build_async", build_async)
    exec(compile(example(), str(README), "exec"), {"__name__": "__main__"})

    lines = capsys.readouterr().out.splitlines()
    assert len(built) == 1  # the example builds its model once, through lite-model
    assert lines[0] == "completed It is 12:00 in Paris."
    assert lines[1:] == [
        "1 session_started",
        "2 run_started",
        "3 user_message",
        "4 model_turn",
        "5 tool_started",
        "6 tool_result",
        "7 model_turn",
        "8 run_finished",
    ]
