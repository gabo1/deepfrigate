CREATE TABLE IF NOT EXISTS recording_segments (
    id text PRIMARY KEY,
    site_id text NOT NULL,
    camera_id text NOT NULL,
    start_time timestamptz NOT NULL,
    end_time timestamptz NOT NULL,
    duration double precision NOT NULL,
    local_path text NOT NULL,
    s3_key text NOT NULL UNIQUE,
    etag text,
    size_bytes bigint NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recording_segments_range_idx
    ON recording_segments (site_id, camera_id, start_time, end_time);
