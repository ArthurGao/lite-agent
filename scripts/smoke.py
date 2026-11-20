"""The installed wheel works from a clean venv (CI `package` job): lite-agent imports, and the
lite-model it brought along has `turn()`. Run with `python -I` from outside the repo."""

import asyncio
from importlib.metadata import version

from lite_model import AssistantTurn, Message, Text
from lite_model.testing import AsyncFakeModel

import lite_agent


async def main() -> None:
    model = AsyncFakeModel(agent=AssistantTurn((Text("ok"),)))
    reply = await model.turn("agent", [Message("user", "ping")], [])
    assert reply.turn.text == "ok", reply
    print(f"lite-agent {version('lite-agent')} on lite-model {version('lite-model')}: ok")


if __name__ == "__main__":
    assert lite_agent.__doc__
    asyncio.run(main())
