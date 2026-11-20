"""lite-agent: an agent harness library (docs/lite-agent-design.md)."""

from lite_agent.agent import Agent
from lite_agent.budget import Budget
from lite_agent.errors import (
    AgentError,
    EventRejected,
    HarnessError,
    SeqConflict,
    StoreError,
    UnknownSession,
    UnresolvedCalls,
)
from lite_agent.projection import Projection, derive
from lite_agent.result import RunError, RunResult
from lite_agent.runner import InProcessRunner, Runner
from lite_agent.store import InMemorySessionStore, SessionStore
from lite_agent.tools import FunctionTool, ModelTool, Tool, ToolContext, tool

__all__ = [
    "Agent",
    "AgentError",
    "Budget",
    "EventRejected",
    "FunctionTool",
    "HarnessError",
    "InMemorySessionStore",
    "InProcessRunner",
    "ModelTool",
    "Projection",
    "RunError",
    "RunResult",
    "Runner",
    "SeqConflict",
    "SessionStore",
    "StoreError",
    "Tool",
    "ToolContext",
    "UnknownSession",
    "UnresolvedCalls",
    "derive",
    "tool",
]
