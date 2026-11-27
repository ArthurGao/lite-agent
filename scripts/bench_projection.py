"""S5b timings (design §16 "Cost of the projection", S4a's note on one recursive CTE): how long
`derive()` takes on a session of N events, and how long the PostgreSQL store's tail read (what
the run does before every request) and full read (what it does when a run starts) take, on a
plain session and at the end of a fork chain. Not a test and not in CI.

    uv run --env-file .env python scripts/bench_projection.py

Uses the database in LITEAGENT_TEST_DATABASE_URL and a schema of its own, `lite_agent_bench`,
which it drops before and after.
"""

import asyncio
import os
import statistics
import time
from datetime import UTC, datetime

import psycopg
from psycopg import sql

from lite_agent.events import (
    Event,
    ModelTurn,
    ProvenanceData,
    RunStarted,
    SessionStarted,
    TextBlock,
    ToolCallBlock,
    ToolResultEvent,
    ToolStarted,
    UsageData,
    UserMessage,
)
from lite_agent.pg import PgSessionStore, migrate
from lite_agent.projection import derive

SCHEMA = "lite_agent_bench"
T0 = datetime(2026, 11, 21, tzinfo=UTC)
WHO = ProvenanceData(
    role="agent",
    requested="qwen3:8b",
    answered="qwen3:8b",
    backend="ollama",
    channel="local",
    routed=False,
)


def session(sid: str, n: int) -> list[Event]:
    """About `n` events: one run of tool turns, each a call, its start, its result."""
    env = {"session_id": sid, "time": T0, "run_id": "r1"}
    events: list[Event] = [
        SessionStarted(seq=1, **env),
        RunStarted(seq=2, role="agent", system="You are a test.", tools=(), **env),
        UserMessage(seq=3, text="Look up every order.", **env),
    ]
    i = 0
    while len(events) < n:
        seq = len(events) + 1
        cid = f"c{i}"
        events += [
            ModelTurn(
                seq=seq,
                blocks=(
                    TextBlock(text="Looking it up."),
                    ToolCallBlock(id=cid, name="get_order", arguments={"order_id": f"A{i}"}),
                ),
                stop="tool_calls",
                role="agent",
                provenance=WHO,
                usage=UsageData(input=900, output=40),
                **env,
            ),
            ToolStarted(
                seq=seq + 1, call_id=cid, name="get_order", arguments={"order_id": f"A{i}"}, **env
            ),
            ToolResultEvent(
                seq=seq + 2,
                call_id=cid,
                content=(row := f"order A{i}: desk lamp, 42.00, shipped (customer C1)"),
                executed=True,
                original_chars=len(row),
                **env,
            ),
        ]
        i += 1
    return events


def ms(samples: list[float]) -> str:
    return f"median {statistics.median(samples) * 1000:7.2f} ms, max {max(samples) * 1000:7.2f} ms"


def time_derive(n: int, repeats: int = 20) -> str:
    events = session("s", n)
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        derive(events)
        samples.append(time.perf_counter() - started)
    return ms(samples)


async def time_reads(url: str, n: int, repeats: int = 20) -> tuple[str, str, str]:
    store = PgSessionStore(url, schema=SCHEMA)
    try:
        sid = f"plain{n}"
        events = session(sid, n)
        for start in range(0, len(events), 500):  # one transaction per 500 events
            await store.append(sid, events[start : start + 500], expected_seq=start)
        # three forks, each at a point 10 below the last and with no events of its own: the tail
        # read at the end of the chain resolves all three
        child = sid
        for depth in range(3):
            child = await store.fork(child, at_seq=n - 10 * (depth + 1))
        tail, full, chained = [], [], []
        for _ in range(repeats):
            started = time.perf_counter()
            await store.read(sid, after_seq=len(events) - 3)
            tail.append(time.perf_counter() - started)
            started = time.perf_counter()
            await store.read(sid)
            full.append(time.perf_counter() - started)
            started = time.perf_counter()
            await store.read(child, after_seq=n - 33)
            chained.append(time.perf_counter() - started)
        return ms(tail), ms(full), ms(chained)
    finally:
        await store.close()


def reset(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))


async def main() -> None:
    url = os.environ["LITEAGENT_TEST_DATABASE_URL"]
    reset(url)
    with psycopg.connect(url, autocommit=True) as conn:
        migrate(conn, SCHEMA)
    try:
        for n in (200, 1000, 5000):
            tail, full, chained = await time_reads(url, n)
            print(f"{n:>5} events  derive      {time_derive(n)}")
            print(f"{'':>12} tail read   {tail}")
            print(f"{'':>12} full read   {full}")
            print(f"{'':>12} fork chain  {chained}  (tail read, 3 forks deep)")
    finally:
        reset(url)


if __name__ == "__main__":
    asyncio.run(main())
