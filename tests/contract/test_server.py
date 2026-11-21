"""The HTTP shell (design §10, S6) and the agent registry it serves: called in process through
httpx.ASGITransport, so no socket is opened; models are lite-model's fake."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from lite_agent import InMemorySessionStore, SessionStore
from lite_agent.registry import Registry, RegistryError, load_agents
from lite_agent.server import check_host, create_app
from tests.contract import server_agents

Make = Callable[..., SessionStore]


HELD = "held_model"


@pytest.fixture
async def client(make_store: Make) -> AsyncIterator[httpx.AsyncClient]:
    store = make_store()
    held = server_agents.Held()
    registry = Registry(
        store,
        {
            "clock": server_agents.clock(store),
            "payer": server_agents.payer(store),
            "held": server_agents.held(store, held),
        },
    )
    app = create_app(registry, poll_s=0.01)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8730") as c:
        setattr(c, HELD, held)
        yield c
    held.release.set()
    await app.state.drain()  # before the store closes


async def finished(c: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    """The session's events once its run has ended: the stream closes when it has."""
    r = await c.get(f"/sessions/{session_id}/events/stream")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    data = [
        line.removeprefix("data: ") for line in r.text.splitlines() if line.startswith("data: ")
    ]
    return [json.loads(d) for d in data]


async def test_a_run_is_started_and_followed_to_its_end(client: httpx.AsyncClient) -> None:
    r = await client.post("/runs", json={"agent": "clock", "task": "Time?"})
    assert r.status_code == 202
    started = r.json()
    streamed = await finished(client, started["session_id"])

    assert [e["type"] for e in streamed] == [
        "session_started", "run_started", "user_message", "model_turn",
        "tool_started", "tool_result", "model_turn", "run_finished",
    ]  # fmt: skip
    assert {e["run_id"] for e in streamed} == {started["run_id"]}
    listed = (await client.get(f"/sessions/{started['session_id']}/events")).json()
    assert listed == streamed  # the JSON view and the stream are the same events
    after = (
        await client.get(f"/sessions/{started['session_id']}/events", params={"after": 6})
    ).json()
    assert [e["seq"] for e in after] == [7, 8]


async def test_a_paused_run_is_resumed_only_at_the_tail_and_only_once(
    client: httpx.AsyncClient,
) -> None:
    sid = (await client.post("/runs", json={"agent": "payer", "task": "Pay."})).json()["session_id"]
    events = await finished(client, sid)
    assert events[-1]["outcome"] == "paused"
    tail = events[-1]["seq"]

    stale = await client.post(
        f"/sessions/{sid}/resume",
        json={"agent": "payer", "expected_seq": tail - 1, "approve": {"c1": True}},
    )
    wrong_call = await client.post(
        f"/sessions/{sid}/resume",
        json={"agent": "payer", "expected_seq": tail, "approve": {"c9": True}},
    )
    ok = await client.post(
        f"/sessions/{sid}/resume",
        json={"agent": "payer", "expected_seq": tail, "approve": {"c1": True}},
    )
    after = await finished(client, sid)
    again = await client.post(
        f"/sessions/{sid}/resume",
        json={"agent": "payer", "expected_seq": after[-1]["seq"], "approve": {"c1": True}},
    )

    assert (stale.status_code, wrong_call.status_code, ok.status_code) == (409, 422, 202)
    assert after[-1]["outcome"] == "completed"
    assert again.status_code == 422  # the call has its result: nothing waits for a decision
    assert stale.json() == {"detail": f"session {sid}: expected seq {tail - 1}, tail is {tail}"}


async def test_one_run_at_a_time_per_session(client: httpx.AsyncClient) -> None:
    # the first run waits on its model until released: it is certainly still going
    sid = (await client.post("/runs", json={"agent": "held", "task": "Wait."})).json()["session_id"]
    second = await client.post("/runs", json={"agent": "held", "task": "Again?", "session_id": sid})
    assert second.status_code == 409
    getattr(client, HELD).release.set()
    await finished(client, sid)
    third = await client.post("/runs", json={"agent": "held", "task": "Again?", "session_id": sid})
    assert third.status_code == 202


async def test_a_fork_and_the_errors_a_client_can_make(client: httpx.AsyncClient) -> None:
    sid = (await client.post("/runs", json={"agent": "clock", "task": "Time?"})).json()[
        "session_id"
    ]
    await finished(client, sid)
    fork = await client.post(f"/sessions/{sid}/fork", json={"at_seq": 3})
    assert fork.status_code == 200
    child = fork.json()["session_id"]
    assert [e["seq"] for e in (await client.get(f"/sessions/{child}/events")).json()] == [1, 2, 3]

    assert (await client.post("/runs", json={"agent": "nobody", "task": "x"})).status_code == 404
    assert (await client.get("/sessions/nowhere/events")).status_code == 404
    assert (
        await client.post("/runs", json={"agent": "clock", "task": "x", "session_id": "nowhere"})
    ).status_code == 404
    assert (await client.post(f"/sessions/{sid}/fork", json={"at_seq": 99})).status_code == 422
    assert (
        await client.post("/runs", json={"agent": "clock", "task": "x", "tools": []})
    ).status_code == 422


async def test_the_agents_and_their_tools_are_listed(client: httpx.AsyncClient) -> None:
    listed = (await client.get("/agents")).json()
    assert [a["name"] for a in listed] == ["clock", "payer", "held"]
    assert [t["name"] for t in listed[0]["tools"]] == ["get_time"]


def test_the_shell_binds_only_to_loopback() -> None:
    for host in ("127.0.0.1", "::1", "127.0.0.2"):
        check_host(host)
    # a name is refused: it can resolve to anything (Codex review 2026-11-21)
    for host in ("0.0.0.0", "192.168.1.5", "example.com", "localhost"):
        with pytest.raises(ValueError, match="loopback"):
            check_host(host)


# --- the registry from agents.toml ---


# The factory cases name a store: without one it is postgres, which reads the environment (and
# `.env`), so the case would test the machine, not the factory (red in CI on 2026-11-21).
MEMORY = '[store]\nkind = "memory"\n'


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "agents.toml"
    path.write_text(body)
    return path


def test_a_registry_is_read_from_agents_toml(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[store]
kind = "memory"
[agents.clock]
factory = "tests.contract.server_agents:clock"
[agents.payer]
factory = "tests.contract.server_agents:payer"
""",
    )
    registry = load_agents(path)
    assert sorted(registry.agents) == ["clock", "payer"]
    assert isinstance(registry.sessions, InMemorySessionStore)
    assert all(a.sessions is registry.sessions for a in registry.agents.values())


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[store]\nkind = "memory"\n', "no agents"),
        (
            '[store]\nkind = "redis"\n[agents.a]\nfactory = "tests.contract.server_agents:clock"\n',
            "store",
        ),
        (MEMORY + '[agents.a]\nfactory = "tests.contract.nowhere:clock"\n', "cannot import"),
        (
            MEMORY + '[agents.a]\nfactory = "tests.contract.server_agents:missing"\n',
            "cannot import",
        ),
        (MEMORY + '[agents.a]\nfactory = "no-colon"\n', "module:function"),
        (
            MEMORY + '[agents.a]\nfactory = "tests.contract.server_agents:not_an_agent"\n',
            "not an Agent",
        ),
        (
            MEMORY + '[agents.a]\nfactory = "tests.contract.server_agents:own_store"\n',
            "shared store",
        ),
        ('[agents.a]\nfactory = "tests.contract.server_agents:clock"\ntools = []\n', "unknown key"),
    ],
)
def test_a_broken_agents_toml_fails_when_it_is_loaded(
    tmp_path: Path, body: str, match: str
) -> None:
    with pytest.raises(RegistryError, match=match):
        load_agents(write(tmp_path, body))


def test_a_registry_built_in_code_needs_one_shared_store() -> None:
    store = InMemorySessionStore()
    with pytest.raises(RegistryError, match="shared store"):
        Registry(store, {"own": server_agents.own_store(store)})
    assert asyncio.iscoroutinefunction(server_agents.clock(store).run)


async def test_two_resumes_at_once_one_is_refused(client: httpx.AsyncClient) -> None:
    # Codex review 2026-11-21: the check and the start are one step, no await between
    sid = (await client.post("/runs", json={"agent": "payer", "task": "Pay."})).json()["session_id"]
    tail = (await finished(client, sid))[-1]["seq"]
    body = {"agent": "payer", "expected_seq": tail, "approve": {"c1": True}}
    both = await asyncio.gather(
        client.post(f"/sessions/{sid}/resume", json=body),
        client.post(f"/sessions/{sid}/resume", json=body),
    )
    assert sorted(r.status_code for r in both) == [202, 409]
    assert (await finished(client, sid))[-1]["outcome"] == "completed"


async def test_two_runs_at_once_on_one_session_one_is_refused(client: httpx.AsyncClient) -> None:
    sid = (await client.post("/runs", json={"agent": "clock", "task": "Time?"})).json()[
        "session_id"
    ]
    await finished(client, sid)
    body = {"agent": "clock", "task": "Again?", "session_id": sid}
    both = await asyncio.gather(client.post("/runs", json=body), client.post("/runs", json=body))
    assert sorted(r.status_code for r in both) == [202, 409]


def test_a_factory_or_a_store_that_fails_is_a_registry_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raising = write(
        tmp_path,
        '[store]\nkind = "memory"\n[agents.a]\nfactory = "tests.contract.server_agents:raises"\n',
    )
    with pytest.raises(RegistryError, match="agent 'a': the factory raised RuntimeError"):
        load_agents(raising)
    monkeypatch.delenv("LITEAGENT_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    no_db = write(tmp_path, '[agents.a]\nfactory = "tests.contract.server_agents:clock"\n')
    with pytest.raises(RegistryError, match="no database configured"):
        load_agents(no_db)


async def test_a_run_that_could_not_be_started_releases_its_session(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex review 2026-11-21 (second pass): a claim never outlives a run that did not start
    sid = (await client.post("/runs", json={"agent": "clock", "task": "Time?"})).json()[
        "session_id"
    ]
    await finished(client, sid)

    def refuse(coro: Any) -> Any:
        coro.close()
        raise RuntimeError("the loop is closing")

    monkeypatch.setattr("lite_agent.server.asyncio.create_task", refuse)
    body = {"agent": "clock", "task": "Again?", "session_id": sid}
    with pytest.raises(RuntimeError):
        await client.post("/runs", json=body)
    monkeypatch.undo()
    assert (await client.post("/runs", json=body)).status_code == 202


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "evil.example"},  # DNS rebinding: a page whose name now resolves to loopback
        {"host": "127.0.0.1:8730", "origin": "https://evil.example"},  # a page in a browser
    ],
)
async def test_a_request_from_a_web_page_is_refused(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    # security review 2026-11-21: no authentication, so only loopback names and no foreign origin
    r = await client.post("/runs", json={"agent": "clock", "task": "Time?"}, headers=headers)
    assert r.status_code in (400, 403)
    assert (await client.get("/agents", headers=headers)).status_code in (400, 403)


async def test_loopback_names_and_the_shells_own_origin_are_served(
    client: httpx.AsyncClient,
) -> None:
    for host in ("127.0.0.1:8730", "localhost:8730", "[::1]:8730"):
        assert (await client.get("/agents", headers={"host": host})).status_code == 200
    own = {"host": "127.0.0.1:8730", "origin": "http://127.0.0.1:8730"}
    assert (await client.get("/agents", headers=own)).status_code == 200


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:3000", "http://localhost:8730", "https://127.0.0.1:8730", "null"]
)
async def test_another_origin_on_this_machine_is_refused(
    client: httpx.AsyncClient, origin: str
) -> None:
    # security review 2026-11-21 (second): any loopback origin was too broad, a local dev
    # server's page could call the shell; only the shell's own origin is taken
    headers = {"host": "127.0.0.1:8730", "origin": origin}
    r = await client.post("/runs", json={"agent": "clock", "task": "Time?"}, headers=headers)
    assert r.status_code == 403


async def test_a_post_that_is_not_json_is_refused(client: httpx.AsyncClient) -> None:
    # a form or text/plain POST needs no CORS preflight: a page could send one blind
    body = '{"agent": "clock", "task": "Time?"}'
    for kind in ("text/plain", "application/x-www-form-urlencoded", None):
        headers = {"content-type": kind} if kind else {}
        r = await client.post("/runs", content=body, headers=headers)
        assert r.status_code == 415, kind
