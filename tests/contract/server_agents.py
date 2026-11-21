"""Agent factories for the registry and HTTP shell tests
(`factory = "tests.contract.server_agents:clock"`). Each takes the shared store and builds an
agent on it, scripted with lite-model's fake."""

import asyncio
from typing import Any

from lite_model.testing import AsyncFakeModel

from lite_agent import Agent, SessionStore
from tests.helpers import Ask, Recording, call, say


def clock(sessions: SessionStore) -> Agent:
    model = AsyncFakeModel(
        agent=lambda asked: [call(("c1", "get_time", {})), say("Noon.")][
            0 if len(asked.messages) == 1 else 1
        ]
    )
    return Agent(
        role="agent", model=model, tools=(Recording().tool("get_time", "12:00"),), sessions=sessions
    )


def payer(sessions: SessionStore) -> Agent:
    model = AsyncFakeModel(
        agent=lambda asked: [call(("c1", "pay", {"amount": 5})), say("Paid.")][
            0 if len(asked.messages) == 1 else 1
        ]
    )
    return Agent(
        role="agent",
        model=model,
        tools=(Recording().tool("pay", "receipt"),),
        gates=(Ask(),),
        sessions=sessions,
    )


def not_an_agent(sessions: SessionStore) -> str:
    return "nope"


def own_store(sessions: SessionStore) -> Agent:
    return Agent(role="agent", model=AsyncFakeModel(agent=say("x")))  # ignores the shared store


def raises(sessions: SessionStore) -> Agent:
    raise RuntimeError("the factory is broken")


class Held:
    """A model whose first reply waits until the test releases it: a run that is still going."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.inner = AsyncFakeModel(agent=lambda asked: say("Done."))

    async def turn(self, role: str, messages: Any, tools: Any) -> Any:
        await self.release.wait()
        return await self.inner.turn(role, messages, tools)


def held(sessions: SessionStore, model: Held) -> Agent:
    return Agent(role="agent", model=model, sessions=sessions)
