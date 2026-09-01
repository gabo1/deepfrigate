# DeepFrigate

GPU video analytics platform built around DeepStream 9, Triton, TensorRT and
Frigate-derived object lifecycle concepts.

## Current milestone

DeepStream detection is on for `tienda`. Frigate `detect.enabled` is false so
Explore is filled by the Event Engine bridge: YOLO26/NvTracker boxes and paths
are written into native Frigate Events. Review/Explore popups use Frigate's
own snapshot (with bbox) and tracking-details path overlay.

## Development setup

1. Copy `.env.example` to `.env` and replace the development database password.
2. Authenticate Docker to NGC before enabling `triton` or `video-engine`.
3. Start supporting services with `sg docker -c 'docker compose up -d mqtt postgres qdrant platform-api detection-adapter frigate'`.
   Frigate's authenticated UI is exposed on `WEB_PORT` (3002 by default).
4. The GPU services require the model plan and an NGC-compatible image. Start
   the video service only with `--profile video` after those checks pass.

Physical camera URLs are defined once through `RTSP_TIENDA`. Compose injects
the value into Frigate as a secrets-compatible environment variable; it is not
duplicated in the DeepStream configuration.
The unmodified Frigate checkout remains under `frigate/`; runtime state,
credentials and its database persist under `config/frigate`, while recordings
use the `frigate-media` volume.

`recording-sync` and MinIO are **deprecated**. Do not deploy them. The
uploader and `recording_segments` index stay in-tree under Compose profile
`deprecated` only.

Frigate's authenticated port is the only application endpoint exposed on every
host interface. MQTT, PostgreSQL, Qdrant, Triton, Platform API, Frame Store,
go2rtc's API, and the optional host RTSP restream bind to `127.0.0.1`; services
communicate through the private Compose network.

## Frontend development

The production Frigate image bakes a patched Vite build. For UI work, do **not**
rebuild that image: keep Frigate running and start a hot-reload overlay that
proxies `/api` to the authenticated UI.

```bash
sg docker -c 'docker compose --env-file .env.example --profile web-dev up frigate-web-dev'
```

The overlay is at `http://127.0.0.1:5173`. Edits under
`services/frigate/web/*.tsx` reload without recreating Frigate. The same script
works on the host if Node 20 is available:

```bash
./scripts/dev-frontend.sh
```

Rebuild `deepfrigate-frigate:local` only when you want the baked UI for a
shared or production-like deploy.

## Detection adapter

The `detection-adapter` consumes the full or minimal DeepStream JSON schema from
`deepfrigate/detections/#`. The batched pipeline publishes to the common
`deepfrigate/detections` topic and identifies each source through msgconv sensor
metadata; legacy per-camera input topics remain supported. It publishes to
`deepfrigate/tracked-objects/{camera_id}` and derives `START`, `UPDATE`, `LOST`
and `END` transitions.

`LOST_AFTER_SECONDS` defaults to 2 seconds and `END_AFTER_SECONDS` to 10
seconds. Zones live in `config/zones.json`; the bbox bottom-center is tested
against each polygon, following Frigate semantics. `ZONE_DWELL_UPDATE_SECONDS`
controls periodic dwell updates and defaults to one second.

Current zones are `area_cajas` for `tienda` persons. Zone messages use
`update_type: "zone"` with `zone_enter`, `dwell_time`, or `zone_exit`, plus
`current_zones` and cumulative `entered_zones`.

Run the adapter's isolated tests with:

```bash
docker build --target test -t deepfrigate-detection-adapter-test \
  -f services/detection-adapter/Dockerfile .
docker run --rm deepfrigate-detection-adapter-test
```

## Frame Store

The `frame-store` service registers transport-independent `FrameRef` handles
defined by `contracts/frame-ref.schema.json`. It supports POSIX SHM references
for CPU/Python consumers and opaque CUDA references scoped to a process,
container or host. Pixels remain in shared memory; MQTT carries lifecycle
metadata only.

The HTTP API on port `8083` provides registration, lookup by ID or track,
consumer acquire/release leases, and owner deletion. TTL expiry and MQTT
`END` events both force cleanup. GPU services and `ai-router` currently use
host IPC so SHM names resolve across their containers.

CUDA references are contracted and lifecycle-managed, but CUDA IPC is not
enabled across the current containers. The operational path uses SHM:
`video-engine` clones RGB buffers into a bounded worker queue, registers crops,
and releases superseded owner leases. By default it selects `person` and `car`,
allows one confidence-driven replacement per second, and refreshes every five
seconds.

`ai-router` uses four workers to look up the latest FrameRef for each tracked
object, acquire a consumer lease, map/read the crop, and release it. Real
two-camera validation sustained about 10 FPS per camera, used roughly 1.5 MiB
of a 7.4 GiB `/dev/shm`, and observed a roughly 0.76 s median
registration-to-consumption age across the final 14-sample run.

## Vehicle embeddings

`models/vehicle-embedding` serves the official PP-ShiTuV2
`general_PPLCNetV2_base_pretrained_v1.0` recognition backbone through Triton's
TensorRT FP16 backend. Run `models/vehicle-embedding/prepare_model.sh` to
generate the checksum-verified ONNX artifact, then `build_engine.sh` on the
target GPU. Generated models remain ignored because TensorRT plans are
runtime/GPU-specific and the upstream weights need separate redistribution
clearance; details are in `models/vehicle-embedding/README.md`.

For `car` tracks, `ai-router` resizes RGB crops to 224x224, applies ImageNet
normalization, requests a 512-dimensional embedding from Triton, performs L2
normalization, and upserts one point per active track into the
`vehicle_embeddings` Qdrant collection. MQTT receives only an `embedding`
update containing the vector ID and latency metadata, never the vector or
pixels. OpenCV `INTER_LINEAR` matches PaddleClas preprocessing, and enrichment
is capped at three successful inferences per active track. Crops include 10%
bbox context and must be at least 48x32 pixels by default. The checked-in
benchmark and similarity scripts reproduce latency and Qdrant consistency
checks.

Run the AI router tests with:

```bash
docker build --target test -t deepfrigate-ai-router-test \
  -f services/ai-router/Dockerfile .
docker run --rm deepfrigate-ai-router-test
```

Run the Frame Store tests with:

```bash
docker build --target test -t deepfrigate-frame-store-test \
  -f services/frame-store/Dockerfile .
docker run --rm deepfrigate-frame-store-test
```

## Event Engine

`event-engine` consumes `tracked_object_update` messages and turns lifecycle,
zone, dwell, stationary, specific-plate, and visual-match inputs into stable
domain events. UUIDv5 IDs make QoS 1 redelivery idempotent; all dwell updates
for one zone visit update a single PostgreSQL row.

Events are stored in PostgreSQL and published to
`deepfrigate/events/{camera_id}` only after persistence succeeds. The Platform
API exposes `GET /v1/events` with camera, type, time and limit filters, plus
`GET /v1/events/{id}`. Event payloads follow
`contracts/event.schema.json`.

For `person` and `car` lifecycles, the same durable worker creates and ends
Frigate manual events through its internal API. PostgreSQL stores the
DeepFrigate-to-Frigate ID mapping, so MQTT redelivery does not create another
Review item. Frigate then performs its normal Review aggregation and renders
the events in its existing authenticated Review and Timeline views. Configure
the mirrored labels with `FRIGATE_REVIEW_LABELS`.

The derived Frigate image keeps the upstream application intact and adds one
`/deepfrigate` route. Its object browser groups recent platform events and
shows lifecycle history, zones, and Qdrant embedding metadata. API calls use
`/api/deepfrigate/*`, which nginx protects with the existing Frigate session;
the Platform API remains bound to localhost and is not remotely reachable
directly.

Objects with an embedding expose a visual search action backed by Qdrant cosine
nearest-neighbor search. `GET /v1/objects/{object_id}/similar` filters
candidates to the same label, excludes the source object, and returns raw
scores plus metadata. The object detail action opens these PP-ShiTuV2 results
inside Frigate Explore. Platform API resolves the source and candidate
`object_id` values through `frigate_event_links`, hydrates the matching Frigate
events, and returns its native `SearchResult` contract. Explore therefore
reuses the existing grid, thumbnails, detail dialog and confidence display.
The UI deliberately treats these scores as visual similarities:
`threshold_validated` remains false until a varied, identity-labeled vehicle
dataset is available.

## AI model management

Frigate Settings includes a DeepFrigate AI Models page backed by
`GET /v1/models`. It reads the live Triton repository, configuration and
statistics APIs to show model family, version, state, GPU placement, tensor
contracts, batching and inference metrics. Required pipeline models can be
loaded or reloaded by an administrator but cannot be unloaded. Unloading other
models is disabled by default and requires
`MODEL_MANAGEMENT_ALLOW_UNLOAD=true`.

## Declarative pipelines

The active DeepStream topology is declared in
`services/video-engine/config/pipeline.yaml` using the versioned
`contracts/pipeline.schema.json` contract. It selects cameras through
environment references, detector model/version, tracker configuration,
frame-export labels, enrichments and rules without storing RTSP credentials
in YAML.

At startup, `video-engine` validates the document, resolves camera sources,
checks rule references against `config/zones.json`, verifies models and
versions against the Triton repository, and enforces one GPU across the current
batched pipeline. Invalid configuration fails before GStreamer is constructed.
The compiled SHA-256, camera IDs and selected components are logged without
exposing source URLs. The validated secret-free source contract is available
from `GET /deepfrigate/v1/pipelines/active`; changes require a service restart.

The Visual Workflow editor is available under
`Settings → DeepFrigate → Visual Workflow`. It renders the active camera,
detector, tracker, FrameRef, enrichment and zone-rule graph. Administrators can
edit, validate and persist the contract; viewers have read-only access.
Saving uses the active SHA-256 as an optimistic concurrency token and never
restarts GPU services automatically.

Run the declarative pipeline tests with:

```bash
docker build --target test -t deepfrigate-video-engine-test \
  -f services/video-engine/Dockerfile .
docker run --rm deepfrigate-video-engine-test
```

Run the Platform API workflow tests with:

```bash
docker build --target test -t deepfrigate-platform-api-test \
  services/platform-api
docker run --rm deepfrigate-platform-api-test
```

Run the Event Engine tests with:

```bash
docker build --target test -t deepfrigate-event-engine-test \
  -f services/event-engine/Dockerfile .
docker run --rm deepfrigate-event-engine-test
```

Checked-in Triton model configurations are contracts. Generated `model.plan`
and `model.onnx` artifacts are excluded; use each model's preparation workflow
to reproduce them.

## Layout

- `frigate/`: unmodified Frigate upstream reference checkout.
- `services/`: new platform services.
- `models/`: Triton model repository.
- `contracts/`: NVIDIA-independent metadata contracts.
- `config/`: Docker service configuration.
