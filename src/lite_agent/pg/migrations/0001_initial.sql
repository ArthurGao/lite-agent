-- lite-agent's session store (design §4, §15 S4). Runs with search_path = <schema>.

-- A session, and where it was forked from: a fork reads its parent's events up to fork_seq
-- and copies none of them.
CREATE TABLE sessions (
    id          text PRIMARY KEY,
    parent_id   text REFERENCES sessions (id),
    fork_seq    integer CHECK (fork_seq >= 1),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((parent_id IS NULL) = (fork_seq IS NULL))
);

-- The log: one row per event, `data` the event as events.dump() gives it. Append-only: the
-- triggers below refuse every UPDATE, DELETE and TRUNCATE, so what a run has checked once
-- cannot change behind it (design §4).
CREATE TABLE events (
    session_id  text NOT NULL REFERENCES sessions (id),
    seq         integer NOT NULL CHECK (seq >= 1),
    type        text NOT NULL,
    v           integer NOT NULL,
    data        jsonb NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE FUNCTION events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'lite-agent events are append-only: % refused', TG_OP;
END
$$;

CREATE TRIGGER events_no_update_or_delete BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_append_only();
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION events_append_only();
