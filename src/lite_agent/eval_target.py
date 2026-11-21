"""The S5 evaluation's lite-eval target (design §15; needs the `eval` extra).

One case is one task of `lite_agent.evals.TASKS`, run on a fresh shop. The output is the run's
metrics, read from its event log (`Metrics`); a scorer only reads `done`. The model is lite-model's
fake replaying each task's scripted correct path (`backend: fake`, deterministic, the CI run) or a
local Ollama model (`backend: ollama`, run several times per case)."""

import hashlib
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from lite_eval.contracts import Case, Output, Span, Trace, Usage
from lite_model import AsyncToolModel, ModelsConfig, RolePolicy, RoleSpec, build_async
from lite_model.testing import AsyncFakeModel

import lite_agent
from lite_agent.evals.tasks import BY_ID, SYSTEM, TASKS, Task, run_task, say

if TYPE_CHECKING:
    from lite_eval.datasets.local import Dataset

Backend = Literal["fake", "ollama"]
Effort = Literal["none", "minimal", "low", "medium", "high"]  # lite-model's ReasoningEffort
ROLES = RoleSpec({"agent": RolePolicy(), "compact": RolePolicy()})


def code_version() -> str:
    """A hash of lite-agent's own code and data: the loop, the projection, the tasks with their
    checks, budgets and system prompt, the shop, the skills. lite-eval keys its cache on
    versions(), so an output is never served for code it was not produced by, released or not
    (Codex review 2026-11-21: the package version alone missed working-tree changes)."""
    root = Path(lite_agent.__file__).parent
    files = sorted(f for f in root.rglob("*") if f.suffix in (".py", ".md", ".sql") and f.is_file())
    digest = hashlib.sha256()
    for f in files:
        digest.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


class AgentTarget:
    """lite-eval target: `{"task": <id>}` in, the run's `Metrics` out."""

    name = "lite-agent"

    def __init__(
        self, backend: Backend = "fake", model: str = "fake", reasoning_effort: Effort = "none"
    ) -> None:
        self.backend = backend
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._live: AsyncToolModel | None = None

    @property
    def deterministic(self) -> bool:
        return self.backend == "fake"

    def config(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }

    def versions(self) -> dict[str, str]:
        return {
            "lite-agent": version("lite-agent"),
            "lite-model": version("lite-model"),
            "code": code_version(),
        }

    def info(self) -> dict[str, str]:
        return {"host": "local"}

    def billing(self) -> Literal["free", "per_token", "unknown"]:
        return "free"  # the fake, or a model on this machine

    def setup_identity(self) -> dict[str, Any]:
        return {self.name: self.config()}

    async def setup(self, dataset: "Dataset") -> None:
        if self.backend == "ollama":
            config = ModelsConfig(
                backend="ollama", model=self.model, reasoning_effort=self.reasoning_effort
            )
            self._live = build_async(config, ROLES)

    def _model(self, task: Task) -> AsyncToolModel:
        if self._live is not None:
            return self._live
        summary = say(task.summary or "Summary.")
        return AsyncFakeModel(agent=list(task.script), compact=lambda asked: summary)

    async def run(self, case: Case) -> Output:
        assert isinstance(case.input, dict)
        task = BY_ID[str(case.input["task"])]
        run = await run_task(task, self._model(task))
        m = run.metrics
        spans = [Span(kind="event", name=e.type, attrs={"seq": e.seq}) for e in run.events]
        usage = Usage(input_tokens=m.input_tokens, output_tokens=m.output_tokens)
        trace = Trace(spans=spans, usage=usage, latency_ms=m.seconds * 1000, latency_scope="task")
        return Output(case_id=case.id, value=m.as_dict(), trace=trace)

    async def teardown(self) -> None:
        self._live = None


def agent(
    backend: Backend = "fake", model: str = "fake", reasoning_effort: Effort = "none"
) -> AgentTarget:
    """The suites' entry point (`python: lite_agent.eval_target:agent`)."""
    return AgentTarget(backend, model, reasoning_effort)


__all__ = ["SYSTEM", "TASKS", "AgentTarget", "agent"]
