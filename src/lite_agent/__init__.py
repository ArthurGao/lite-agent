"""lite-agent: an agent harness library (docs/lite-agent-design.md)."""

from lite_agent.agent import Agent
from lite_agent.budget import Budget
from lite_agent.compaction import Compactor, SummaryCompactor
from lite_agent.context import Memory, NotesMemory, SkillDir, SkillInfo, SkillSource
from lite_agent.errors import (
    AgentError,
    EventRejected,
    HarnessError,
    SeqConflict,
    StoreError,
    UnknownSession,
    UnresolvedCalls,
)
from lite_agent.gates import Allow, Deny, Gate, PauseFor, RunContext
from lite_agent.masking import Masker, ToolResultMasker
from lite_agent.projection import Projection, derive
from lite_agent.result import Pause, Resume, RunError, RunResult, ToolOutcome, Waiting
from lite_agent.runner import InProcessRunner, Runner
from lite_agent.store import InMemorySessionStore, SessionStore, copy_session
from lite_agent.tools import FunctionTool, ModelTool, Tool, ToolContext, tool

__all__ = [
    "Agent",
    "AgentError",
    "Allow",
    "Budget",
    "Compactor",
    "Deny",
    "EventRejected",
    "FunctionTool",
    "Gate",
    "HarnessError",
    "InMemorySessionStore",
    "InProcessRunner",
    "Masker",
    "Memory",
    "ModelTool",
    "NotesMemory",
    "Pause",
    "PauseFor",
    "Projection",
    "Resume",
    "RunContext",
    "RunError",
    "RunResult",
    "Runner",
    "SeqConflict",
    "SessionStore",
    "SkillDir",
    "SkillInfo",
    "SkillSource",
    "StoreError",
    "SummaryCompactor",
    "Tool",
    "ToolContext",
    "ToolOutcome",
    "ToolResultMasker",
    "UnknownSession",
    "UnresolvedCalls",
    "Waiting",
    "copy_session",
    "derive",
    "tool",
]
