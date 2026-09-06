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

-- Cross-camera transitions inferred from PP-ShiTu embeddings within a time
-- window (services/event-engine/app/transitions.py). One row per arriving
-- track (to_object_id): the best earlier match on a paired camera.
CREATE TABLE IF NOT EXISTS camera_transitions (
    id uuid PRIMARY KEY,
    from_camera text NOT NULL,
    to_camera text NOT NULL,
    from_object_id text NOT NULL,
    to_object_id text NOT NULL UNIQUE,
    from_frigate_event_id text,
    to_frigate_event_id text,
    label text NOT NULL,
    from_seen_at timestamptz NOT NULL,
    to_seen_at timestamptz NOT NULL,
    gap_seconds double precision NOT NULL,
    score double precision NOT NULL,
    from_vector_id text,
    to_vector_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS camera_transitions_pair_time_idx
    ON camera_transitions (from_camera, to_camera, to_seen_at DESC);
CREATE INDEX IF NOT EXISTS camera_transitions_to_seen_idx
    ON camera_transitions (to_seen_at DESC);

ALTER TABLE camera_transitions ADD COLUMN IF NOT EXISTS method text NOT NULL DEFAULT 'embedding';
ALTER TABLE camera_transitions ADD COLUMN IF NOT EXISTS candidates integer NOT NULL DEFAULT 1;
ALTER TABLE camera_transitions ALTER COLUMN score DROP NOT NULL;
