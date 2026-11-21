"""Resume and fork across processes (design §15 S4b): every step runs in a new Python process
(`agent_process.py`) that shares nothing with the one before it but the PostgreSQL schema, so
nothing a run relies on can be left in a process's memory (AGENTS.md rule 4)."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from lite_agent.pg import PgSessionStore
from tests.helpers import kinds, pg_read

ROOT = Path(__file__).resolve().parents[2]


def step(
    url: str, schema: str, name: str, session_id: str = "", **env: str
) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, "-m", "tests.contract.agent_process", url, schema, name]
    # a clean environment: nothing reaches the child but what it needs to start
    base = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        args + ([session_id] if session_id else []),
        cwd=ROOT,
        env=base | env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def reply(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr[-2000:]
    out: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return out


async def kinds_in(url: str, schema: str, session_id: str) -> list[str]:
    return kinds(await pg_read(url, schema, session_id))


async def test_a_run_paused_in_one_process_is_approved_and_finished_in_another(
    pg_url: str, pg_schema: str
) -> None:
    paused = reply(step(pg_url, pg_schema, "pause"))
    assert (paused["outcome"], paused["waiting"], paused["ran"]) == ("paused", ["approval"], [])

    done = reply(step(pg_url, pg_schema, "approve", paused["session"]))

    assert (done["outcome"], done["text"], done["ran"]) == ("completed", "Paid.", ["pay"])
    assert (await kinds_in(pg_url, pg_schema, paused["session"]))[-6:] == [
        "run_started",
        "resumed",
        "tool_started",
        "tool_result",
        "model_turn",
        "run_finished",
    ]


async def test_a_process_that_died_inside_a_tool_leaves_a_call_the_next_one_reconciles(
    pg_url: str, pg_schema: str
) -> None:
    crashed = step(pg_url, pg_schema, "call", CRASH_IN_TOOL="1")
    assert crashed.returncode == 3 and crashed.stdout == ""
    store = PgSessionStore(pg_url, schema=pg_schema)
    try:
        (session_id,) = [s.id for s in await store.sessions()]
    finally:
        await store.close()
    assert (await kinds_in(pg_url, pg_schema, session_id))[-1] == "tool_started"

    # the tool is not idempotent: the next process must not run it again, it asks
    reopened = reply(step(pg_url, pg_schema, "reopen", session_id))
    assert (reopened["outcome"], reopened["waiting"], reopened["ran"]) == (
        "paused",
        ["reconcile"],
        [],
    )
    settled = reply(step(pg_url, pg_schema, "settle", session_id))
    assert (settled["outcome"], settled["text"], settled["ran"]) == ("completed", "Paid.", [])


async def test_a_session_forked_in_one_process_runs_on_in_another(
    pg_url: str, pg_schema: str
) -> None:
    first = reply(step(pg_url, pg_schema, "ask"))
    child = reply(step(pg_url, pg_schema, "fork", first["session"]))["child"]

    done = reply(step(pg_url, pg_schema, "continue", child))

    assert (done["session"], done["outcome"], done["text"]) == (child, "completed", "Child.")
    parent = await kinds_in(pg_url, pg_schema, first["session"])
    assert len(parent) == 5 and len(await kinds_in(pg_url, pg_schema, child)) == 9
