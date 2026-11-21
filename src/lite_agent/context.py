"""What is put in front of the model besides the task (design §6): skills, an index in the
system prompt and a tool that loads one on demand; and memory, two tools and a sentence saying
what it is for. All of it reaches the model through the log: the assembled system prompt in
`run_started`, everything else as an ordinary tool result."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from lite_agent.tools import Tool, ToolContext, tool

SKILLS = (
    "Skills are instructions for particular kinds of work. When a task matches one, call "
    "`load_skill` with its name to read it before you start.\n{index}"
)
# Says what memory is for and leaves the judgement to the model: no rules on what or when to
# record (KB llm-app-architecture §13.1).
MEMORY = (
    "You have a memory that outlasts this conversation. `recall` looks up what was noted "
    "earlier; `remember` keeps a note for later conversations, for what is worth keeping."
)
NOTHING_NOTED = "Nothing is noted about that."


# --- skills ---


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str  # one line: all the model sees of a skill until it loads it


class SkillSource(Protocol):
    def index(self) -> Sequence[SkillInfo]: ...

    def load(self, name: str) -> str:
        """The skill's full text. KeyError for a name that is not in the index."""
        ...


def _parse(path: Path) -> tuple[SkillInfo, str]:
    """A `SKILL.md`: a frontmatter block with `name` and `description`, then the body. Only
    those two keys are read, as plain single-line values (no quoting, no folded text), so no
    YAML parser is needed."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()  # an editor's BOM is not content
    end = next((i for i, line in enumerate(lines) if i and line.strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        raise ValueError(f"{path}: no frontmatter block (--- ... ---) at the top")
    fields: dict[str, str] = {}
    for key, _, value in (line.partition(":") for line in lines[1:end]):
        if key.strip() in fields and key.strip() in ("name", "description"):
            raise ValueError(f"{path}: the frontmatter gives {key.strip()} more than once")
        fields[key.strip()] = value.strip()
    for key in ("name", "description"):
        if not fields.get(key):
            raise ValueError(f"{path}: the frontmatter has no {key}")
    if fields["name"] != path.parent.name:
        raise ValueError(f"{path}: name {fields['name']!r} does not match its directory")
    body = "\n".join(lines[end + 1 :]).strip()
    return SkillInfo(fields["name"], fields["description"]), body


class SkillDir:
    """Skills as `<root>/<name>/SKILL.md`, the layout Claude Code uses. Read once, when it is
    built: a malformed skill fails then, not in the middle of a run."""

    def __init__(self, root: Path | str) -> None:
        parsed = [_parse(p) for p in sorted(Path(root).glob("*/SKILL.md"))]
        self._index = tuple(info for info, _ in parsed)
        self._bodies = {info.name: body for info, body in parsed}

    def index(self) -> Sequence[SkillInfo]:
        return self._index

    def load(self, name: str) -> str:
        return self._bodies[name]


class _LoadSkill(BaseModel):
    name: str


def skill_tool(source: SkillSource) -> Tool:
    @tool(description="Read a skill's full instructions by its name.", idempotent=True)
    async def load_skill(args: _LoadSkill, ctx: ToolContext) -> str:
        try:
            return source.load(args.name)
        except KeyError:
            known = ", ".join(s.name for s in source.index())
            raise ValueError(
                f"there is no skill named {args.name!r}; the skills are: {known}"
            ) from None

    return load_skill


# --- memory ---


class Memory(Protocol):
    async def recall(self, query: str) -> str:
        """What is known about `query`, as text for the model."""
        ...

    async def remember(self, note: str) -> None: ...


_WORD = re.compile(r"[^\W_]+")  # letters and digits: punctuation and hyphens separate words


class NotesMemory:
    """Notes in a list, found by words they share with the query. For tests and one-process
    scripts: it is not a retrieval engine and keeps nothing after the process ends."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    async def recall(self, query: str) -> str:
        words = set(_WORD.findall(query.lower()))
        found = [n for n in self.notes if words & set(_WORD.findall(n.lower()))]
        return "\n".join(f"- {n}" for n in found) or NOTHING_NOTED

    async def remember(self, note: str) -> None:
        self.notes.append(note)


class _Recall(BaseModel):
    query: str = Field(pattern=r"\S")


class _Remember(BaseModel):
    note: str = Field(pattern=r"\S")  # something other than whitespace


def memory_tools(memory: Memory) -> tuple[Tool, Tool]:
    @tool(description="Look up what was noted earlier about something.", idempotent=True)
    async def recall(args: _Recall, ctx: ToolContext) -> str:
        return await memory.recall(args.query)

    @tool(description="Keep a note for later conversations.")
    async def remember(args: _Remember, ctx: ToolContext) -> str:
        await memory.remember(args.note)
        return "Noted."

    return recall, remember


# --- what an agent is built with ---


@dataclass(frozen=True)
class Context:
    """The system prompt and the built-in tools, fixed when the agent is built: what the budget
    was checked against is what every run sends and logs, whatever the sources do later."""

    system: str
    tools: tuple[Tool, ...]


def build(system: str, skills: SkillSource | None, memory: Memory | None, *, limit: int) -> Context:
    """The host's system prompt, then the skills index, then the memory note; and the tools
    that go with them (`load_skill`; `recall`, `remember`). A pure function of its inputs: the
    same agent gives the same prompt, byte for byte (the prompt cache depends on it). An index
    longer than `limit` characters is refused, never cut."""
    parts, tools = [system], []
    if skills is not None:
        # sorted here, so the prompt does not depend on the order a source happens to give
        listed = sorted(skills.index(), key=lambda s: s.name)
        index = "\n".join(f"- {s.name}: {s.description}" for s in listed)
        if len(index) > limit:
            raise ValueError(
                f"the skills index is {len(index)} characters, over the {limit} allowed"
            )
        parts.append(SKILLS.format(index=index))
        tools.append(skill_tool(skills))
    if memory is not None:
        parts.append(MEMORY)
        tools += memory_tools(memory)
    return Context("\n\n".join(p for p in parts if p), tuple(tools))
