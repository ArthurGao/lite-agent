-- An inbox event whose run kept failing for longer than its worker retries one item (a poison
-- message) is set aside as `failed`, kept for a person to look at, so the events behind it go on
-- (design §9.1). 0002 is applied in places: the status check is replaced, not edited there.
ALTER TABLE inbox_events DROP CONSTRAINT inbox_events_status_check;
ALTER TABLE inbox_events ADD CONSTRAINT inbox_events_status_check
    CHECK (status IN ('pending', 'done', 'refused', 'failed'));
