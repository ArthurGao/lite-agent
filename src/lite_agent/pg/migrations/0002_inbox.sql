-- The inbox and the timers of always-on agents (design §9.1, S7b; KB llm-app-architecture §12).
-- Runs with search_path = <schema>.

-- Work ready now: one row per event from any source (a webhook, a schedule, a person's reply),
-- deduplicated on the source's own id, so a source that delivers twice is recorded once.
CREATE TABLE inbox_events (
    id          bigserial PRIMARY KEY,
    inbox       text NOT NULL,
    agent       text NOT NULL,            -- the agent the inbox's worker runs: who to wake
    source      text NOT NULL,
    source_id   text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('message', 'decision')),
    session_id  text,                     -- a continuation of this session; none: a new one
    payload     jsonb NOT NULL,           -- message: {"text"}; decision: {"approve", "results"}
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'refused')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    done_at     timestamptz,
    run_session text,                     -- the session the event's run used
    UNIQUE (inbox, source, source_id),
    CHECK (kind = 'message' OR session_id IS NOT NULL)  -- a decision answers a session
);
CREATE INDEX inbox_events_pending ON inbox_events (inbox, id) WHERE status = 'pending';

-- Deadlines an agent set itself (follow_up, once) and recurring schedules (every_s seconds).
-- They are not work: when due, the scheduler writes an inbox event and moves on (§12.2).
CREATE TABLE timers (
    id          bigserial PRIMARY KEY,
    inbox       text NOT NULL,
    agent       text NOT NULL,            -- the agent the inbox's worker runs
    kind        text NOT NULL CHECK (kind IN ('follow_up', 'schedule')),
    due_at      timestamptz NOT NULL,
    every_s     integer CHECK (every_s > 0),
    text        text NOT NULL,
    session_id  text,                     -- a follow-up continues the session that set it
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done')),
    CHECK ((kind = 'schedule') = (every_s IS NOT NULL))
);
CREATE INDEX timers_due ON timers (due_at) WHERE status = 'active';
