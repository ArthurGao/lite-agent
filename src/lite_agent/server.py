"""The HTTP shell (design §10, S6; the `server` extra): what a host in another language uses.
It holds no logic of its own (AGENTS.md rule 10): it checks a request, then hands it to `Agent`.

    POST /runs                       {agent, task, session_id?}       -> 202 {session_id, run_id}
    GET  /sessions/{id}/events       ?after=N                         -> the events, as stored
    GET  /sessions/{id}/events/stream?after=N                         -> SSE until the run ends
    POST /sessions/{id}/resume       {agent, expected_seq, approve?, results?} -> 202
    POST /sessions/{id}/fork         {at_seq}                         -> {session_id}
    GET  /agents                                                      -> names and tool schemas

A run goes on in the background; the client follows its events. One run per session at a time
(409 otherwise); a resume names the tail it saw (`expected_seq`), and a decision or a result for
a call that is not waiting for one is refused (422), never written over. Bind to loopback only:
there is no authentication (design §10).
"""

import asyncio
import ipaddress
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field

from lite_agent.agent import Agent
from lite_agent.errors import EventRejected, UnknownSession
from lite_agent.events import Event, ToolDef, dump
from lite_agent.loop import _check_resume
from lite_agent.projection import unanswered_calls
from lite_agent.registry import Registry
from lite_agent.result import Resume, RunResult, ToolOutcome
from lite_agent.store import new_id

log = logging.getLogger("lite_agent.server")


def check_host(host: str) -> None:
    """Refuse any address that is not loopback: the shell has no authentication yet. Only a
    numeric address is taken: a name, even `localhost`, can resolve to anything."""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError(f"liteagent serve binds to loopback only (no authentication yet), not {host}")


def _loopback_name(name: str | None) -> bool:
    """A name a loopback request can carry in Host or Origin: localhost or a loopback address."""
    if not name:
        return False
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False


def _host_name(host: str) -> str:
    """`127.0.0.1:8730` -> `127.0.0.1`, `[::1]:8730` -> `::1`, `localhost` -> `localhost`."""
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a request cannot bring tools or anything else


class StartRun(_Body):
    agent: str
    task: str = Field(min_length=1)
    session_id: str | None = None  # the next run of this session; none: a new session


class Outcome(_Body):
    content: str
    is_error: bool = False
    unknown: bool = False


class ResumeRun(_Body):
    agent: str
    expected_seq: int = Field(ge=0)
    approve: dict[str, bool] = Field(default_factory=dict)
    results: dict[str, Outcome] = Field(default_factory=dict)


class ForkAt(_Body):
    at_seq: int = Field(ge=1)


def create_app(registry: Registry, *, poll_s: float = 1.0) -> FastAPI:
    """The shell over `registry`. `poll_s`: how often a stream reads the log's tail."""
    app = FastAPI(title="lite-agent", summary="An agent harness over HTTP (design §10)")

    @app.middleware("http")
    async def only_from_this_machine(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Bound to loopback, but a web page can still reach it: by rebinding its own name to
        # 127.0.0.1 (its Host is then that name) or by sending from the browser (its Origin is
        # the page's). With no authentication, only loopback names are served.
        host = request.headers.get("host", "")
        if not _loopback_name(_host_name(host)):
            return JSONResponse({"detail": "only loopback host names are served"}, status_code=400)
        # a page from anywhere else, another port on this machine included, is a foreign origin
        origin = request.headers.get("origin")
        if origin is not None and origin != f"http://{host}":
            return JSONResponse({"detail": "requests from web pages are refused"}, status_code=403)
        # a JSON body needs a CORS preflight a foreign page never passes; a form does not
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if request.method == "POST" and kind != "application/json":
            return JSONResponse({"detail": "send application/json"}, status_code=415)
        return await call_next(request)

    store = registry.sessions
    # A session is claimed from the moment a request for a run on it passes the check (no await
    # in between, so two requests cannot both pass) until that run ends. In-process only: across
    # processes the store's expected-seq check is what refuses the second writer.
    claimed: set[str] = set()
    running: set[asyncio.Task[None]] = set()  # for drain()

    def agent_named(name: str) -> Agent:
        agent = registry.get(name)
        if agent is None:
            raise HTTPException(404, f"no agent {name}")
        return agent

    async def events_of(session_id: str, after: int = 0, *, ours: bool = True) -> list[Event]:
        """The session's events; 404 if there is none. With `ours`, a session a run here has
        claimed but not yet written reads as empty (a stream opened right after POST /runs)."""
        try:
            return list(await store.read(session_id, after_seq=after))
        except UnknownSession:
            if ours and session_id in claimed:
                return []
            raise HTTPException(404, f"no session {session_id}") from None

    def claim(session_id: str) -> None:
        if session_id in claimed:
            raise HTTPException(409, f"session {session_id} has a run going on")
        claimed.add(session_id)

    def start(session_id: str, work: Callable[[], Awaitable[RunResult]]) -> None:
        async def go() -> None:
            try:
                await work()
            except Exception:  # the run's own failures are in its log; this is a harness bug
                log.exception("run in session %s raised", session_id)
            finally:
                claimed.discard(session_id)

        try:
            task = asyncio.create_task(go())
        except BaseException:  # never started: nothing will release the claim but this
            claimed.discard(session_id)
            raise
        running.add(task)
        task.add_done_callback(running.discard)

    @app.post("/runs", status_code=202)
    async def post_run(body: StartRun) -> dict[str, str]:
        agent = agent_named(body.agent)
        run_id = new_id()
        session_id = body.session_id or new_id()
        claim(session_id)
        try:
            given = body.session_id is not None
            if given and unanswered_calls(await events_of(session_id, ours=False)):
                raise HTTPException(409, f"session {session_id} waits for a resume, not a task")
        except BaseException:
            claimed.discard(session_id)
            raise
        new = body.session_id is None
        start(
            session_id,
            lambda: agent.run(body.task, session_id=session_id, run_id=run_id, new_session=new),
        )
        return {"session_id": session_id, "run_id": run_id}

    @app.get("/sessions/{session_id}/events")
    async def get_events(
        session_id: str, after: Annotated[int, Query(ge=0)] = 0
    ) -> list[dict[str, Any]]:
        return [dump(e) for e in await events_of(session_id, after)]

    @app.get("/sessions/{session_id}/events/stream", response_class=EventSourceResponse)
    async def stream_events(
        session_id: str, after: Annotated[int, Query(ge=0)] = 0
    ) -> AsyncIterator[ServerSentEvent]:
        # whether a run goes on is read before the tail: a run that ends meanwhile is read once more
        going = session_id in claimed
        batch = await events_of(session_id, after)  # a 404 before the stream opens
        while True:
            for e in batch:
                yield ServerSentEvent(data=dump(e), event=e.type, id=str(e.seq))
                after = e.seq
            if not going:
                return
            await asyncio.sleep(poll_s)
            going = session_id in claimed
            batch = await events_of(session_id, after)

    @app.post("/sessions/{session_id}/resume", status_code=202)
    async def post_resume(session_id: str, body: ResumeRun) -> dict[str, str]:
        agent = agent_named(body.agent)
        claim(session_id)
        try:
            events = await events_of(session_id, ours=False)
            tail = events[-1].seq if events else 0
            if body.expected_seq != tail:
                raise HTTPException(
                    409, f"session {session_id}: expected seq {body.expected_seq}, tail is {tail}"
                )
            given = Resume(
                approve=dict(body.approve),
                results={
                    k: ToolOutcome(v.content, v.is_error, v.unknown)
                    for k, v in body.results.items()
                },
            )
            try:
                _check_resume(events, given)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        except BaseException:
            claimed.discard(session_id)
            raise
        run_id = new_id()
        start(session_id, lambda: agent.resume(session_id, given, run_id=run_id))
        return {"session_id": session_id, "run_id": run_id}

    @app.post("/sessions/{session_id}/fork")
    async def post_fork(session_id: str, body: ForkAt) -> dict[str, str]:
        try:
            child = await store.fork(session_id, at_seq=body.at_seq)
        except UnknownSession:
            raise HTTPException(404, f"no session {session_id}") from None
        except EventRejected as exc:
            raise HTTPException(422, str(exc)) from None
        return {"session_id": child}

    @app.get("/agents")
    async def get_agents() -> list[Mapping[str, Any]]:
        return [
            {
                "name": name,
                "role": agent.role,
                # as a run offers them to the model (`run_started.tools`)
                "tools": [
                    ToolDef.model_validate(t, from_attributes=True).model_dump()
                    for t in agent.all_tools
                ],
            }
            for name, agent in registry.agents.items()
        ]

    async def drain() -> None:
        """Wait for the runs still going (at shutdown: none is cut off mid-turn)."""
        await asyncio.gather(*running, return_exceptions=True)

    app.state.drain = drain
    app.router.on_shutdown.append(drain)
    return app
