"""lite-agent's own errors. A tool's exception or a model failure is part of a run and is
recorded in the log; these mean the harness itself cannot go on (design §5 rule 7)."""


class AgentError(Exception):
    """Base of every lite-agent error."""


class EventRejected(AgentError):
    """An event that may not enter or leave the log: invalid, of an unknown type, or written by
    a newer vocabulary (design §4)."""


class StoreError(AgentError):
    """The session store failed to read or append."""


class SeqConflict(StoreError):
    """`expected_seq` is not the session's tail: another writer appended first (design §4)."""


class UnknownSession(StoreError):
    """No session with this id."""


class UnresolvedCalls(AgentError):
    """The session's last turn asked for tool calls that have no result: a run was interrupted
    between a call and its answer. Sending that history would be rejected, and a tool that may
    already have run must not be repeated blindly, so `run()` refuses the session (design §8;
    S2's `resume()` settles each call)."""

    def __init__(self, session_id: str, call_ids: tuple[str, ...]) -> None:
        super().__init__(f"session {session_id} has unanswered tool calls: {', '.join(call_ids)}")
        self.session_id = session_id
        self.call_ids = call_ids


class HarnessError(AgentError):
    """An invariant of the harness does not hold (the log's projection is not what was built)."""
