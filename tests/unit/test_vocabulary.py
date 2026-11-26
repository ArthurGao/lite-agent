"""The event vocabulary is frozen per version (design §4, from v0.1.0): stores keep logs, so an
event's shape may change only together with `V`, and the code keeps reading the old one. This
fingerprints every event type's JSON Schema for the current `V`; a shape change that leaves `V`
alone fails here (Codex review of the v0.1.0 release)."""

import hashlib
import json
from typing import cast

from pydantic import BaseModel

from lite_agent.events import EVENT_TYPES, V

# One fingerprint per vocabulary version. Changing an event's shape: bump V in events.py, keep
# reading the old version, and add the new version's fingerprint below; never edit an old one.
FROZEN = {
    1: "f35f152a1fd02b1b",
    2: "506737d964eb32f6",
}  # v0.1.0, 2026-11-21; V=2: tool_results_masked


def fingerprint() -> str:
    schemas = {t.__name__: cast(type[BaseModel], t).model_json_schema() for t in EVENT_TYPES}
    return hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()[:16]


def test_the_event_shapes_are_the_ones_frozen_for_this_version() -> None:
    assert V in FROZEN, f"vocabulary version {V} has no frozen fingerprint"
    assert fingerprint() == FROZEN[V], (
        "an event's shape changed but V did not: bump V in events.py (and keep reading the old "
        f"version) instead of changing version {V} in place"
    )
