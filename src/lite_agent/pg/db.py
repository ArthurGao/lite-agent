"""PostgreSQL connections and migrations (design §15 S4).

Vendored from lite-cache `src/lite_cache/db.py` @ 6afa032, which is itself vendored from lite-rag
`src/lite_rag/db.py` @ e42fc93 (AGENTS.md rule 8: lite-model is the only sibling imported at run
time). Edits: this docstring; the migrations package (`lite_agent.pg.migrations`); the error
class (`lite_agent.errors.StoreError`); `connection_error`, which takes any exception and shows
only its type and the redacted URL; `redact_conninfo` hides every
`*password*` parameter, not only `password`; the advisory lock prefix (`lite_agent:`); no pgvector
(lite-agent stores no vectors), so `check_vector`, `require_url`, `connect` and the extension
are gone.

Migrations are the `NNNN_*.sql` files in `migrations/`, applied in order, each in its own
transaction, recorded in `<schema>.schema_migrations`. They run with `search_path = <schema>`,
so one set of files serves any schema name.
"""

from importlib import resources

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from lite_agent.errors import StoreError

MIGRATIONS = "lite_agent.pg.migrations"


def connection_error(exc: Exception, url: str) -> StoreError:
    # psycopg's message can quote the connection string: never pass it on (rule 14). Only the
    # exception's type is shown (PoolTimeout, OperationalError...), with the password redacted.
    return StoreError(
        f"cannot connect to PostgreSQL at {redact_conninfo(url)} ({type(exc).__name__})"
    )


def redact_conninfo(value: str) -> str:
    """The connection string with its password replaced, in any form libpq accepts (URL or
    `key=value`). Something that does not parse is never echoed back."""
    try:
        params = conninfo_to_dict(value)
    except psycopg.ProgrammingError:
        return "<unparseable connection string>"
    for key in params:  # password, sslpassword: every credential libpq takes
        if "password" in key and params[key]:
            params[key] = "***"
    return make_conninfo("", **params)


def migration_files(package: str = MIGRATIONS) -> list[tuple[str, str]]:
    """(version, SQL) of each `NNNN_*.sql` file in `package`, in order."""
    files = resources.files(package)
    names = sorted(f.name for f in files.iterdir() if f.name.endswith(".sql"))
    return [(name.removesuffix(".sql"), files.joinpath(name).read_text("utf-8")) for name in names]


def use_schema(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))


def migrate(conn: psycopg.Connection, schema: str, until: str | None = None) -> list[str]:
    """Apply pending migrations to `schema` (up to and including `until` when given); returns
    the versions applied (empty: up to date)."""
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            ).format(sql.Identifier(schema))
        )
    applied: list[str] = []
    for version, script in migration_files():
        if until is not None and version > until:
            break
        with conn.transaction():
            use_schema(conn, schema)
            # Serialise concurrent migrators on this schema; the check below then sees their work.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"lite_agent:{schema}",))
            done = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
            ).fetchone()
            if done:
                continue
            conn.execute(script.encode())  # trusted SQL file shipped in the package
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


def pending(conn: psycopg.Connection, schema: str) -> list[str]:
    """The migration versions not yet applied to `schema` (all of them when it has none)."""
    table = sql.Identifier(schema, "schema_migrations")
    applied: set[str] = set()
    with conn.transaction():
        row = conn.execute("SELECT to_regclass(%s)", (table.as_string(conn),)).fetchone()
        if row and row[0] is not None:
            rows = conn.execute(sql.SQL("SELECT version FROM {}").format(table))
            applied = {version for (version,) in rows}
    return [v for v, _ in migration_files() if v not in applied]
