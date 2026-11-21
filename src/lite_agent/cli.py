"""`liteagent`: look at and fork sessions in the PostgreSQL store, and apply its migrations
(design §15 S4b). It holds no logic (AGENTS.md rule 10): it reads the settings, calls the
library and prints. There is no `run` or `resume`: a run needs the host's tools, gates and
completion condition, which are Python objects a command line cannot give."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import ValidationError

from lite_agent.config import NO_DATABASE, Settings
from lite_agent.errors import AgentError
from lite_agent.events import (
    Event,
    ModelTurn,
    RunFinished,
    RunStarted,
    TextBlock,
    ToolCallBlock,
    ToolResultEvent,
    ToolStarted,
    UserMessage,
    dump,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {_safe(message)}", err=True)  # messages quote ids, which are the host's
    return typer.Exit(1)


if TYPE_CHECKING:
    from lite_agent.pg import PgSessionStore


def _settings() -> Settings:
    """The settings, or one line naming the bad one (never its value: it may be the URL)."""
    try:
        return Settings()
    except ValidationError as exc:
        first = exc.errors()[0]
        name = str(first["loc"][0]) if first["loc"] else "?"
        name = name if name.startswith("LITEAGENT_") else f"LITEAGENT_{name.upper()}"
        raise _fail(f"invalid setting {name}: {first['msg']}") from None


def _safe(text: str) -> str:
    """Text a terminal shows as is: every control character written as its escape."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)


def _pg() -> ModuleType:
    try:
        import lite_agent.pg as pg
    except ImportError as exc:  # psycopg is the `pg` extra
        raise _fail("the CLI needs the pg extra: install lite-agent[pg]") from exc
    return pg


def _with_store[T](use: Callable[["PgSessionStore"], Awaitable[T]]) -> T:
    """Run `use(store)` on a store from the settings, closing it afterwards; a library error
    becomes one line on stderr and exit code 1."""

    settings = _settings()

    async def go() -> T:
        store: PgSessionStore = _pg().PgSessionStore.from_settings(settings)
        try:
            return await use(store)
        finally:
            await store.close()

    try:
        return asyncio.run(go())
    except AgentError as exc:
        raise _fail(str(exc)) from None


@app.command()
def migrate() -> None:
    """Apply the store's pending migrations to LITEAGENT_SCHEMA."""
    pg, settings = _pg(), _settings()
    if not settings.database_url:
        raise _fail(NO_DATABASE)
    import psycopg

    try:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            applied = pg.migrate(conn, settings.schema_name)
    except psycopg.Error as exc:  # any of them may quote the connection string
        raise _fail(str(pg.connection_error(exc, settings.database_url))) from None
    schema = settings.schema_name
    typer.echo(
        f"applied {', '.join(applied)} to {schema}" if applied else f"{schema} is up to date"
    )


@app.command()
def sessions(limit: Annotated[int, typer.Option(min=1)] = 50) -> None:
    """The newest sessions first: last seq, the last run's outcome, where a fork came from."""
    rows = _with_store(lambda store: store.sessions(limit))
    typer.echo(f"{'session':<34} {'events':>6}  {'last outcome':<12}  {'forked from':<22}  created")
    for r in rows:
        forked = f"{r.parent_id}@{r.fork_seq}" if r.parent_id else "-"
        created = r.created_at.isoformat(timespec="seconds")
        row = f"{r.id:<34} {r.tail:>6}  {r.last_outcome or '-':<12}  {forked:<22}  {created}"
        typer.echo(_safe(row))  # ids are whatever the host chose


def _summary(e: Event) -> str:
    match e:
        case RunStarted():
            return f"role {e.role}, {len(e.tools)} tool{'' if len(e.tools) == 1 else 's'}"
        case UserMessage():
            return e.text
        case ModelTurn():
            calls = [f"{b.name}({b.id})" for b in e.blocks if isinstance(b, ToolCallBlock)]
            text = " ".join(b.text for b in e.blocks if isinstance(b, TextBlock))
            return f"{e.stop}: {', '.join(calls) if calls else text}"
        case ToolStarted():
            return f"{e.name}({e.call_id})"
        case ToolResultEvent():
            return f"{e.call_id}: {e.content}"
        case RunFinished():
            return f"{e.outcome} ({e.stop})"
    return ""


@app.command()
def show(
    session_id: str,
    as_json: Annotated[bool, typer.Option("--json", help="each event as stored")] = False,
) -> None:
    """Every event of a session, inherited ones first, one per line."""
    events = _with_store(lambda store: store.read(session_id))
    for e in events:
        if as_json:
            # JSON escapes C0 controls itself; DEL and C1 (\x9b is a terminal's CSI) it leaves
            line = json.dumps(dump(e), ensure_ascii=False)
            typer.echo("".join(f"\\u{ord(c):04x}" if 0x7F <= ord(c) <= 0x9F else c for c in line))
        else:
            line = _safe(f"{e.seq:>4}  {e.type:<16} {_summary(e)}").rstrip()
            typer.echo(line if len(line) <= 200 else line[:199] + "…")


@app.command()
def fork(session_id: str, at: Annotated[int, typer.Option("--at", min=1)]) -> None:
    """A new session sharing `session_id`'s events up to seq `--at`; prints its id."""
    typer.echo(_safe(_with_store(lambda store: store.fork(session_id, at_seq=at))))


@app.command()
def config() -> None:
    """The settings in effect, the password hidden."""
    settings = _settings()
    url = settings.database_url
    shown = _pg().redact_conninfo(url) if url else "(not set)"
    typer.echo(f"database_url  {_safe(shown)}")
    typer.echo(f"schema        {settings.schema_name}")


@app.command()
def serve(
    agents: Annotated[Path, typer.Option(help="agents.toml: the agents this server offers")],
    host: Annotated[str, typer.Option(help="loopback only (no authentication yet)")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8730,
) -> None:
    """The HTTP shell (design §10; the server extra): runs, events, resume and fork over HTTP."""
    try:
        from lite_agent.registry import RegistryError, load_agents
        from lite_agent.server import check_host, create_app
    except ImportError as exc:  # FastAPI is the `server` extra
        raise _fail("serve needs the server extra: install lite-agent[server]") from exc
    try:
        check_host(host)
        registry = load_agents(agents)
    except (ValueError, RegistryError) as exc:
        raise _fail(str(exc)) from None
    import uvicorn

    uvicorn.run(create_app(registry), host=host, port=port)


@app.command()
def worker(
    agents: Annotated[Path, typer.Option(help="agents.toml: the agents this worker runs")],
    task_queue: Annotated[str, typer.Option(help="the Temporal task queue to poll")] = "lite-agent",
    target: Annotated[str, typer.Option(help="the Temporal frontend")] = "127.0.0.1:7233",
    namespace: Annotated[str, typer.Option()] = "default",
    inbox: Annotated[
        bool, typer.Option(help="also run the inbox workers (postgres store)")
    ] = False,
    timers: Annotated[bool, typer.Option(help="also start the scheduler (needs --inbox)")] = False,
) -> None:
    """A Temporal worker for AgentWorkflow and its run activity (design §9; the temporal extra);
    with --inbox, the inbox workers too, and with --timers, the scheduler (design §9.1)."""
    try:
        from temporalio.client import Client

        from lite_agent.registry import RegistryError, load_agents
        from lite_agent.temporal import make_worker
    except ImportError as exc:  # temporalio is the `temporal` extra
        raise _fail("worker needs the temporal extra: install lite-agent[temporal]") from exc
    try:
        registry = load_agents(agents)
    except RegistryError as exc:
        raise _fail(str(exc)) from None
    if timers and not inbox:
        raise _fail("--timers needs --inbox: the scheduler only writes to inboxes")
    box = None
    if inbox:
        pg = _pg()
        if not isinstance(registry.sessions, pg.PgSessionStore):
            raise _fail('--inbox needs [store] kind = "postgres": the inbox is a table')
        from lite_agent.pg.inbox import PgInbox

        box = PgInbox(registry.sessions)

    async def go() -> None:
        client = await Client.connect(target, namespace=namespace)
        if timers:
            from lite_agent.temporal import start_timers

            await start_timers(client, task_queue)
        await make_worker(client, task_queue, registry, inbox=box).run()

    asyncio.run(go())


def main() -> None:
    app()
