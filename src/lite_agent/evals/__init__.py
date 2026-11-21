"""The S5 evaluation (design §15): a small, deterministic shop the agent works in, and a fixed
set of tasks whose completion is decided by code. Nothing here calls a model on its own: the
caller passes one (lite-model's fake in CI, a local Ollama model for the baseline)."""

from lite_agent.evals.tasks import TASKS, Metrics, Task, TaskRun, Verdict, run_task
from lite_agent.evals.world import World

__all__ = ["TASKS", "Metrics", "Task", "TaskRun", "Verdict", "World", "run_task"]
