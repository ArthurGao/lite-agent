"""The `liteagent` CLI (design §15 S4b): each command against the test schema, its output
asserted line by line. The CLI only parses input and calls the library (AGENTS.md rule 10)."""

import asyncio
import json
import secrets
from typing import Any

import psycopg
import pytest
from lite_model.testing import AsyncFakeModel
from typer.testing import CliRunner

from lite_agent import Agent
from lite_agent.cli import app
from lite_agent.events import dump
from lite_agent.pg import PgSessionStore
from tests.conftest import TEST_SCHEMA
from tests.helpers import T0, Recording, call, counter, pg_read, say

runner = CliRunner()


@pytest.fixture
def env(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """The CLI's settings point at the test schema, whatever `.env` says."""
    monkeypatch.setenv("LITEAGENT_DATABASE_URL", pg_url)
    monkeypatch.setenv("LITEAGENT_SCHEMA", TEST_SCHEMA)
    return pg_url


async def one_session(url: str) -> None:
    """Session `s1`: a tool call, its result, an answer."""
    store = PgSessionStore(url, schema=TEST_SCHEMA, ids=counter("x"))
    a = Agent(
        role="agent",
        model=AsyncFakeModel(agent=[call(("c1", "get_time", {})), say("It is noon.")]),
        tools=(Recording().tool("get_time", "12:00"),),
        sessions=store,
        clock=lambda: T0,
        ids=counter("s"),
    )
    await a.run("What time is it?")
    await store.close()


def test_migrate_applies_then_says_up_to_date(env: str) -> None:
    with psycopg.connect(env, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    first = runner.invoke(app, ["migrate"])
    second = runner.invoke(app, ["migrate"])
    # S7b added 0002_inbox
    assert (first.exit_code, first.output) == (
        0,
        f"applied 0001_initial, 0002_inbox to {TEST_SCHEMA}\n",
    )
    assert (second.exit_code, second.output) == (0, f"{TEST_SCHEMA} is up to date\n")


def test_show_lists_every_event_in_order(env: str, pg_schema: str) -> None:
    asyncio.run(one_session(env))
    shown = runner.invoke(app, ["show", "s1"])
    assert shown.exit_code == 0, shown.output
    assert shown.output.splitlines() == [
        "   1  session_started",
        "   2  run_started      role agent, 1 tool",
        "   3  user_message     What time is it?",
        "   4  model_turn       tool_calls: get_time(c1)",
        "   5  tool_started     get_time(c1)",
        "   6  tool_result      c1: 12:00",
        "   7  model_turn       end: It is noon.",
        "   8  run_finished     completed (end_turn)",
    ]


def test_show_json_is_each_stored_event_as_it_is_stored(env: str, pg_schema: str) -> None:
    asyncio.run(one_session(env))
    shown = runner.invoke(app, ["show", "s1", "--json"])
    lines = [json.loads(line) for line in shown.output.splitlines()]
    assert lines == [
        json.loads(json.dumps(dump(e))) for e in asyncio.run(pg_read(env, TEST_SCHEMA, "s1"))
    ]


def test_fork_prints_the_new_session_and_sessions_lists_both(env: str, pg_schema: str) -> None:
    asyncio.run(one_session(env))
    forked = runner.invoke(app, ["fork", "s1", "--at", "3"])
    assert forked.exit_code == 0, forked.output
    child = forked.output.strip()
    assert child and child != "s1"

    listed = runner.invoke(app, ["sessions"])
    rows = [line.split() for line in listed.output.splitlines()]
    assert rows[0] == ["session", "events", "last", "outcome", "forked", "from", "created"]
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["s1"][1:4] == ["8", "completed", "-"]
    assert by_id[child][1:4] == ["3", "-", "s1@3"]
    assert len(rows) == 3


def test_an_unknown_session_is_an_error_not_a_traceback(env: str, pg_schema: str) -> None:
    shown = runner.invoke(app, ["show", "nowhere"])
    assert (shown.exit_code, shown.stdout, shown.stderr) == (1, "", "error: no session nowhere\n")


def test_config_prints_the_settings_without_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    password = secrets.token_hex(8)
    monkeypatch.setenv("LITEAGENT_DATABASE_URL", f"postgresql://someone:{password}@db.example/x")
    monkeypatch.setenv("LITEAGENT_SCHEMA", "lite_agent_other")
    shown = runner.invoke(app, ["config"])
    assert shown.exit_code == 0
    assert password not in shown.output
    assert shown.output.splitlines() == [
        "database_url  user=someone password=*** dbname=x host=db.example",
        "schema        lite_agent_other",
    ]


def test_without_a_database_the_cli_says_what_to_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.delenv("LITEAGENT_DATABASE_URL", raising=False)
    monkeypatch.chdir(str(tmp_path))  # no .env here either
    shown = runner.invoke(app, ["sessions"])
    assert (shown.exit_code, shown.stderr) == (
        1,
        "error: no database configured: set LITEAGENT_DATABASE_URL\n",
    )


def test_show_escapes_control_characters_and_marks_a_cut(env: str, pg_schema: str) -> None:
    # Codex review 2026-11-21: stored text must not drive the terminal, and a cut line says so
    task = "Red \x1b[31mtext\x9b2J" + "x" * 300

    async def seed() -> None:
        store = PgSessionStore(env, schema=TEST_SCHEMA)
        a = Agent(
            role="agent",
            model=AsyncFakeModel(agent=[say("Done.")]),
            sessions=store,
            ids=counter("s"),
        )
        await a.run(task)
        await store.close()

    asyncio.run(seed())
    text = runner.invoke(app, ["show", "s1"]).output
    as_json = runner.invoke(app, ["show", "s1", "--json"]).output
    for out in (text, as_json):
        assert "\x1b" not in out and "\x9b" not in out
    line = text.splitlines()[2]
    assert line.startswith("   3  user_message     Red \\x1b[31mtext\\x9b2J")
    assert line.endswith("…") and len(line) == 200
    assert json.loads(as_json.splitlines()[2])["text"] == task  # --json loses nothing


def test_a_bad_setting_is_one_line_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITEAGENT_DATABASE_URL", "postgresql://x@localhost/x")
    monkeypatch.setenv("LITEAGENT_SCHEMA", "public")
    shown = runner.invoke(app, ["sessions"])
    assert shown.exit_code == 1
    assert shown.stderr.startswith("error: invalid setting LITEAGENT_SCHEMA:")
    assert len(shown.stderr.splitlines()) == 1


def test_migrate_never_shows_the_password_whatever_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    password = secrets.token_hex(8)
    # an option libpq refuses: a ProgrammingError, not an OperationalError
    monkeypatch.setenv(
        "LITEAGENT_DATABASE_URL", f"postgresql://u:{password}@127.0.0.1:1/x?nonsense=1"
    )
    monkeypatch.setenv("LITEAGENT_SCHEMA", "lite_agent_test")
    shown = runner.invoke(app, ["migrate"])
    assert shown.exit_code == 1
    assert password not in shown.stderr + shown.stdout + repr(shown.exception)
    assert shown.stderr.startswith("error: ")


def test_ids_and_errors_are_escaped_too(env: str, pg_schema: str) -> None:
    # security review 2026-11-21: a session id is the host's string; `sessions` and every
    # error line print it, so they escape it as `show` does
    bad = "s\x1b]0;owned\x07\x9b2J"

    async def seed() -> None:
        store = PgSessionStore(env, schema=TEST_SCHEMA)
        ids = iter([bad, "r1"])
        a = Agent(
            role="agent",
            model=AsyncFakeModel(agent=[say("Done.")]),
            sessions=store,
            ids=lambda: next(ids),
        )
        await a.run("Hi.")
        await store.close()

    asyncio.run(seed())
    listed = runner.invoke(app, ["sessions"])
    missing = runner.invoke(app, ["show", "nowhere\x1b[2J"])
    for out in (listed.output, missing.stderr):
        assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert listed.output.splitlines()[1].startswith("s\\x1b]0;owned\\x07\\x9b2J")
    assert missing.stderr == "error: no session nowhere\\x1b[2J\n"


def test_serve_refuses_a_non_loopback_host_and_a_bad_agents_file(tmp_path: Any) -> None:
    good = tmp_path / "agents.toml"
    good.write_text(
        '[store]\nkind = "memory"\n[agents.clock]\nfactory = "tests.contract.server_agents:clock"\n'
    )
    bad = tmp_path / "bad.toml"
    bad.write_text('[agents.a]\nfactory = "tests.contract.nowhere:clock"\n')
    public = runner.invoke(app, ["serve", "--agents", str(good), "--host", "0.0.0.0"])
    broken = runner.invoke(app, ["serve", "--agents", str(bad)])
    assert public.exit_code == 1 and "loopback only" in public.stderr
    assert (
        broken.exit_code == 1
        and broken.stderr == "error: agent 'a': cannot import tests.contract.nowhere:clock\n"
    )


def test_worker_refuses_a_bad_agents_file_before_it_connects(tmp_path: Any) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[store]\nkind = "memory"\n[agents.a]\nfactory = "tests.contract.nowhere:clock"\n'
    )
    shown = runner.invoke(app, ["worker", "--agents", str(bad)])
    assert shown.exit_code == 1
    assert shown.stderr == "error: agent 'a': cannot import tests.contract.nowhere:clock\n"


def test_worker_inbox_needs_the_postgres_store_and_timers_need_the_inbox(tmp_path: Any) -> None:
    memory = tmp_path / "agents.toml"
    memory.write_text(
        '[store]\nkind = "memory"\n[agents.clock]\nfactory = "tests.contract.server_agents:clock"\n'
    )
    no_pg = runner.invoke(app, ["worker", "--agents", str(memory), "--inbox"])
    no_inbox = runner.invoke(app, ["worker", "--agents", str(memory), "--timers"])
    assert no_pg.exit_code == 1 and "postgres" in no_pg.stderr
    assert (
        no_inbox.exit_code == 1
        and no_inbox.stderr
        == "error: --timers needs --inbox: the scheduler only writes to inboxes\n"
    )
