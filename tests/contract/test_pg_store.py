"""What only the PostgreSQL store has: the database refuses to rewrite the log, events are read
back through the vocabulary's own check, and connection details stay out of error messages."""

import json
import secrets

import psycopg
import pytest
from psycopg import sql

from lite_agent import EventRejected, StoreError
from lite_agent.config import Settings
from lite_agent.pg import PgSessionStore, migrate, pending
from tests.contract.test_store_contract import said, started


async def opened(pg_url: str, pg_schema: str) -> PgSessionStore:
    store = PgSessionStore(pg_url, schema=pg_schema)
    await store.append("s1", [started(), said(2)], expected_seq=0)
    return store


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE {} SET data = data",
        "DELETE FROM {}",
        "TRUNCATE {} CASCADE",
    ],
    ids=["update", "delete", "truncate"],
)
async def test_the_database_refuses_to_rewrite_events(
    pg_url: str, pg_schema: str, statement: str
) -> None:
    store = await opened(pg_url, pg_schema)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(sql.SQL(statement).format(sql.Identifier(pg_schema, "events")))
    assert await store.read("s1") == [started(), said(2)]
    await store.close()


@pytest.mark.parametrize(
    "change",
    [{"type": "tool_exploded"}, {"v": 99}, {"surprise": 1}],
    ids=["unknown_type", "newer_vocabulary", "extra_field"],
)
async def test_an_event_this_code_cannot_vouch_for_is_refused_when_read(
    pg_url: str, pg_schema: str, change: dict[str, object]
) -> None:
    store = await opened(pg_url, pg_schema)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        row = conn.execute(
            sql.SQL("SELECT data FROM {} WHERE session_id = 's1' AND seq = 2").format(
                sql.Identifier(pg_schema, "events")
            )
        ).fetchone()
        assert row is not None
        bad = json.dumps(row[0] | change | {"seq": 3})
        conn.execute(
            sql.SQL(
                "INSERT INTO {} (session_id, seq, type, v, data) VALUES ('s1', 3, 'x', 1, %s)"
            ).format(sql.Identifier(pg_schema, "events")),
            (bad,),
        )
    with pytest.raises(EventRejected):
        await store.read("s1")
    await store.close()


def test_migrations_are_applied_once_and_recorded(pg_url: str, pg_schema: str) -> None:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        assert pending(conn, pg_schema) == []
        assert migrate(conn, pg_schema) == []  # already there: nothing to do


async def test_a_store_on_a_schema_that_was_never_migrated_says_so(pg_url: str) -> None:
    store = PgSessionStore(pg_url, schema="lite_agent_never_migrated")
    try:
        with pytest.raises(StoreError, match="apply the migrations first"):
            await store.read("s1")
    finally:
        await store.close()


async def test_a_closed_store_says_so_and_does_not_reopen(pg_url: str, pg_schema: str) -> None:
    # Codex review 2026-11-21: after close() every call fails plainly, not on a dead pool
    store = await opened(pg_url, pg_schema)
    await store.close()
    for call in (
        store.read("s1"),
        store.append("s1", [said(3)], expected_seq=2),
        store.fork("s1", at_seq=1),
    ):
        with pytest.raises(StoreError, match="closed"):
            await call
    await store.close()  # closing twice is harmless


def fake_password() -> str:
    """Made at run time: no credentialed URL is written in the source (secret scanners flag
    one, and a fixed value could be special-cased by the code under test)."""
    return secrets.token_hex(8)


def test_every_secret_in_a_connection_string_is_hidden() -> None:
    from lite_agent.pg import redact_conninfo

    first, second = fake_password(), fake_password()
    shown = redact_conninfo(f"postgresql://u:{first}@h/db?sslpassword={second}&sslmode=require")
    assert first not in shown and second not in shown
    assert "sslmode=require" in shown


async def test_a_connection_failure_never_shows_the_password() -> None:
    password = fake_password()
    store = PgSessionStore(f"postgresql://someone:{password}@127.0.0.1:1/nowhere", schema="x_y")
    with pytest.raises(StoreError) as caught:
        await store.read("s1")
    assert password not in str(caught.value) and "***" in str(caught.value)
    await store.close()


@pytest.mark.parametrize("schema", ["public", "pg_catalog", "information_schema", "Bad-Name", ""])
def test_a_schema_that_is_not_lite_agents_own_is_refused(schema: str) -> None:
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://x@localhost/x", schema_name=schema)
    with pytest.raises(ValueError):
        PgSessionStore("postgresql://x@localhost/x", schema=schema)


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"postgresql://a:{fake_password()}@localhost/c"
    monkeypatch.setenv("LITEAGENT_DATABASE_URL", url)
    monkeypatch.setenv("LITEAGENT_SCHEMA", "lite_agent_other")
    settings = Settings()
    assert (settings.database_url, settings.schema_name) == (
        url,
        "lite_agent_other",
    )
    monkeypatch.delenv("LITEAGENT_SCHEMA")
    assert Settings().schema_name == "lite_agent"


async def test_a_second_store_object_sees_what_the_first_wrote(pg_url: str, pg_schema: str) -> None:
    first, second = (
        PgSessionStore(pg_url, schema=pg_schema),
        PgSessionStore(pg_url, schema=pg_schema),
    )
    await first.append("s1", [started(), said(2)], expected_seq=0)
    assert await second.read("s1") == [started(), said(2)]
    await second.append("s1", [said(3)], expected_seq=2)
    assert [e.seq for e in await first.read("s1")] == [1, 2, 3]
    await first.close()
    await second.close()


@pytest.mark.parametrize("limit", [0, -1])
async def test_listing_sessions_needs_a_positive_limit(
    pg_url: str, pg_schema: str, limit: int
) -> None:
    store = PgSessionStore(pg_url, schema=pg_schema)
    try:
        with pytest.raises(ValueError, match="limit"):
            await store.sessions(limit)
    finally:
        await store.close()
