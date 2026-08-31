CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY,
    event_type text NOT NULL,
    object_id text NOT NULL,
    camera_id text NOT NULL,
    track_id bigint NOT NULL CHECK (track_id >= 0),
    occurred_at timestamptz NOT NULL,
    source_update_type text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_occurred_at_idx
    ON events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_camera_occurred_idx
    ON events (camera_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_type_occurred_idx
    ON events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_object_occurred_idx
    ON events (object_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS frigate_event_links (
    start_event_id uuid PRIMARY KEY,
    object_id text NOT NULL,
    camera_id text NOT NULL,
    marker text NOT NULL,
    frigate_event_id text UNIQUE,
    state text NOT NULL CHECK (state IN ('creating', 'active', 'ended')),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS frigate_event_links_active_object_idx
    ON frigate_event_links (object_id, started_at DESC)
    WHERE state <> 'ended';
