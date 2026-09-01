# recording-sync — deprecated (1 Sep 2026)

Do not deploy MinIO or this service. The S3 uploader and
`recording_segments` index are out of scope.

Compose profile: `deprecated`. Default `docker compose up` does not start
them.

Jina historical backfill (`POST /api/events/{id}/thumbnail/embed` over old
Events) is also deprecated. Live Events keep being indexed; older rows may
lack `vec_thumbnails`.
