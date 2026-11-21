"""The agents a server or a worker offers, by name (design §10, §9): built once, at start-up, from
`agents.toml` or in code. A request names an agent; it can never bring tools or code of its own.

    [store]                            # every agent shares one session store
    kind = "postgres"                  # or "memory"; postgres: LITEAGENT_DATABASE_URL, _SCHEMA
    [agents.support]
    factory = "myapp.agents:support"   # def support(sessions: SessionStore) -> Agent

A bad file, a factory that does not import or returns something else, or an agent on a store of
its own fails here, when it is loaded, not at the first request.
"""

import importlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lite_agent.agent import Agent
from lite_agent.errors import AgentError
from lite_agent.store import InMemorySessionStore, SessionStore


class RegistryError(AgentError):
    """agents.toml, or an agent built from it, is not usable."""


@dataclass(frozen=True)
class Registry:
    sessions: SessionStore
    agents: Mapping[str, Agent] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.agents:
            raise RegistryError("no agents registered")
        for name, agent in self.agents.items():
            if not isinstance(agent, Agent):
                raise RegistryError(f"agent {name!r} is not an Agent")
            if agent.sessions is not self.sessions:
                # one store: a session, its forks and its resumes are found wherever they run
                raise RegistryError(f"agent {name!r} is not on the registry's shared store")

    def get(self, name: str) -> Agent | None:
        return self.agents.get(name)


def _store(spec: Mapping[str, Any]) -> SessionStore:
    unknown = set(spec) - {"kind"}
    if unknown:
        raise RegistryError(f"[store]: unknown key {', '.join(sorted(unknown))}")
    kind = spec.get("kind", "postgres")
    if kind == "memory":
        return InMemorySessionStore()
    if kind == "postgres":
        try:
            from lite_agent.pg import PgSessionStore
        except ImportError as exc:  # psycopg is the `pg` extra
            raise RegistryError("[store] kind postgres needs the pg extra: lite-agent[pg]") from exc
        try:
            return PgSessionStore.from_settings()
        except (AgentError, ValueError) as exc:  # no URL, or a setting that does not validate
            raise RegistryError(f"[store]: {exc}") from exc
    raise RegistryError(f"[store]: kind must be memory or postgres, not {kind!r}")


def _factory(name: str, spec: Mapping[str, Any]) -> Any:
    unknown = set(spec) - {"factory"}
    if unknown:
        raise RegistryError(f"agent {name!r}: unknown key {', '.join(sorted(unknown))}")
    target = spec.get("factory")
    if not isinstance(target, str) or target.count(":") != 1:
        raise RegistryError(f"agent {name!r}: factory must be 'module:function'")
    module, _, attr = target.partition(":")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as exc:
        raise RegistryError(f"agent {name!r}: cannot import {target}") from exc


def load_agents(path: Path | str) -> Registry:
    """The registry `agents.toml` at `path` describes, every agent built on its shared store."""
    try:
        config = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot read {path}: {type(exc).__name__}") from exc
    unknown = set(config) - {"store", "agents"}
    if unknown:
        raise RegistryError(f"{path}: unknown key {', '.join(sorted(unknown))}")
    specs = config.get("agents") or {}
    if not specs:
        raise RegistryError(f"{path}: no agents")
    factories = {name: _factory(name, spec) for name, spec in specs.items()}
    sessions = _store(config.get("store") or {})
    agents: dict[str, Agent] = {}
    for name, make in factories.items():
        try:
            built = make(sessions)
        except Exception as exc:  # the host's code: its message is not ours to repeat
            raise RegistryError(f"agent {name!r}: the factory raised {type(exc).__name__}") from exc
        if not isinstance(built, Agent):
            raise RegistryError(
                f"agent {name!r}: the factory returned {type(built).__name__}, not an Agent"
            )
        agents[name] = built
    return Registry(sessions, agents)
