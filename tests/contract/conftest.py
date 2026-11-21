"""Every test here runs against both session stores (design §3: a session produced under one
implementation must behave the same under another)."""

from collections.abc import AsyncIterator, Callable

import pytest

from lite_agent import InMemorySessionStore, SessionStore
from lite_agent.store import new_id

Ids = Callable[[], str]


@pytest.fixture(params=["memory", "postgres"])
async def make_store(
    request: pytest.FixtureRequest,
) -> AsyncIterator[Callable[[Ids], SessionStore]]:
    """A factory: `make_store(ids)` gives a store of the parametrised kind, sessions empty."""

    if request.param == "memory":

        def memory(ids: Ids = new_id) -> SessionStore:
            return InMemorySessionStore(ids=ids)

        yield memory
        return

    from lite_agent.pg import PgSessionStore

    url: str = request.getfixturevalue("pg_url")
    schema: str = request.getfixturevalue("pg_schema")
    stores: list[PgSessionStore] = []

    def postgres(ids: Ids = new_id) -> SessionStore:
        store = PgSessionStore(url, schema=schema, ids=ids)
        stores.append(store)
        return store

    yield postgres
    for store in stores:
        await store.close()
