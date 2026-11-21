"""Shared test setup. Tests never open a socket (AGENTS.md rule 11): only tests marked `ollama`
(opt-in) may reach a real server, and tests marked `temporal` their local test server."""

import socket
from collections.abc import Iterator
from typing import Any

import pytest

# ollama: the real local model (opt-in). temporal: the SDK's own test server, started and
# stopped by the test (it downloads the server once); it runs by default.
LIVE_MARKERS = {"ollama", "temporal"}


def uses_postgres(request: pytest.FixtureRequest) -> bool:
    """A test of the PostgreSQL store: it asks for the test database, directly or as the
    `postgres` case of the store contract."""
    callspec = getattr(request.node, "callspec", None)
    contract = callspec is not None and callspec.params.get("make_store") == "postgres"
    return contract or "pg_url" in request.fixturenames


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if LIVE_MARKERS & {m.name for m in request.node.iter_markers()}:
        return
    if uses_postgres(request):  # the test database only: every other test stays offline
        return

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "sendto", refuse)  # UDP needs no connect()
    monkeypatch.setattr(socket, "getaddrinfo", refuse)  # a DNS lookup is a network call too


# --- PostgreSQL (design §15 S4) ---
# PG tests read their URL only from LITEAGENT_TEST_DATABASE_URL (set explicitly, never inherited
# from the app's URL) and only ever use the schema below, which they drop and recreate
# (AGENTS.md rule 11). Without the variable they fail, not skip:
# run `uv run --env-file .env pytest`.

TEST_SCHEMA = "lite_agent_test"


@pytest.fixture
def pg_url() -> str:
    import os

    url = os.environ.get("LITEAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "LITEAGENT_TEST_DATABASE_URL is not set: PostgreSQL tests need it (see .env.example)"
        )
    return url


@pytest.fixture
def pg_schema(pg_url: str) -> Iterator[str]:
    """A freshly migrated, empty `lite_agent_test` schema (dropped again afterwards)."""
    import psycopg
    from psycopg import sql

    from lite_agent.pg import migrate

    drop = sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(TEST_SCHEMA))
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(drop)
        migrate(conn, TEST_SCHEMA)
    yield TEST_SCHEMA
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(drop)
