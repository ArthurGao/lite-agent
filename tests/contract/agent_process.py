"""One side of a cross-process test (design §15 S4b): a fresh Python process with its own
`Agent` and its own store object, sharing nothing with the test but the database. Scripted with
lite-model's fake, so it calls no model. Prints one JSON object.

    python -m tests.contract.agent_process <url> <schema> <step> [<session id>]
"""

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from lite_model.testing import AsyncFakeModel

from lite_agent import Agent, FunctionTool, Resume, ToolContext, ToolOutcome
from lite_agent.pg import PgSessionStore
from tests.helpers import Ask, call, say

ran: list[str] = []


async def pay(args: Mapping[str, Any], ctx: ToolContext) -> str:
    ran.append("pay")
    if os.environ.get("CRASH_IN_TOOL"):
        os._exit(3)  # the process dies after tool_started was written, before any result
    return "receipt"


def agent(store: PgSessionStore, *replies: Any, gated: bool = False) -> Agent:
    # Not tests.helpers.agent: its counting ids start again in every process and would collide.
    tool = FunctionTool("pay", "Pays.", {"type": "object"}, pay)
    return Agent(
        role="agent",
        model=AsyncFakeModel(agent=list(replies)),
        tools=(tool,),
        gates=(Ask(),) if gated else (),
        sessions=store,
    )


async def main(url: str, schema: str, step: str, session_id: str | None) -> dict[str, Any]:
    store = PgSessionStore(url, schema=schema)
    try:
        if step == "pause":
            r = await agent(store, call(("c1", "pay", {})), gated=True).run("Pay.")
        elif step == "approve":
            assert session_id
            r = await agent(store, say("Paid."), gated=True).resume(
                session_id, Resume(approve={"c1": True})
            )
        elif step == "call":  # with CRASH_IN_TOOL set, this process never returns
            r = await agent(store, call(("c1", "pay", {}))).run("Pay.")
        elif step == "reopen":
            assert session_id
            r = await agent(store).resume(session_id)
        elif step == "settle":
            assert session_id
            r = await agent(store, say("Paid.")).resume(
                session_id, Resume(results={"c1": ToolOutcome("receipt, checked by hand")})
            )
        elif step == "ask":
            r = await agent(store, say("One.")).run("First?")
        elif step == "fork":
            assert session_id
            return {"child": await store.fork(session_id, at_seq=5)}
        elif step == "continue":
            assert session_id
            r = await agent(store, say("Child.")).run("In the fork?", session_id=session_id)
        else:
            raise SystemExit(f"unknown step {step}")
        reasons = [w.reason for w in r.pause.waiting] if r.pause else []
        return {
            "session": r.session_id,
            "outcome": r.outcome,
            "text": r.text,
            "ran": ran,
            "waiting": reasons,
        }
    finally:
        await store.close()


if __name__ == "__main__":
    url, schema, step = sys.argv[1:4]
    session_id = sys.argv[4] if len(sys.argv) > 4 else None
    print(json.dumps(asyncio.run(main(url, schema, step, session_id))))
