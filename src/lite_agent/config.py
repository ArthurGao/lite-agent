"""Settings (design §11): environment variables with the `LITEAGENT_` prefix, or `.env`."""

from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict


def own_schema(name: str) -> str:
    """lite-agent keeps its tables in a schema of its own: never a shared or system one (copied
    from lite-cache `config.own_schema`, itself from lite-mem)."""
    if name in ("public", "information_schema") or name.startswith("pg_"):
        raise ValueError(
            f"schema {name!r} is shared or a system schema: use one of lite-agent's own"
        )
    return name


NO_DATABASE = "no database configured: set LITEAGENT_DATABASE_URL"

# A schema lite-agent owns: a lowercase SQL identifier, not public or a system schema.
OwnSchema = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,62}$"),
    AfterValidator(own_schema),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITEAGENT_", env_file=".env", extra="ignore", populate_by_name=True
    )

    database_url: str | None = None
    # Read only from LITEAGENT_SCHEMA: a plain alias would make pydantic-settings read an
    # unprefixed SCHEMA variable instead.
    schema_name: OwnSchema = Field(default="lite_agent", validation_alias="LITEAGENT_SCHEMA")
