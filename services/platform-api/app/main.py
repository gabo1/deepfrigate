"""HTTP entry point for the DeepFrigate platform API."""

from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response
from jsonschema import Draft202012Validator
from . import heatmap as heatmap_render
import psycopg
from psycopg.rows import dict_row
import yaml

app = FastAPI(title="DeepFrigate Platform API", version="0.1.0")
database_url = os.environ["DATABASE_URL"]
qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
qdrant_collection = os.getenv(
    "QDRANT_COLLECTION", "vehicle_embeddings"
)
frigate_api_url = os.getenv(
    "FRIGATE_API_URL", "http://frigate:5000/api"
).rstrip("/")
triton_url = os.getenv("TRITON_HTTP_URL", "http://triton:8000").rstrip(
    "/"
)
required_models = {
    model.strip()
    for model in os.getenv(
        "TRITON_REQUIRED_MODELS", "object-detector,vehicle-embedding,person-attribute"
    ).split(",")
    if model.strip()
}
allow_model_unload = os.getenv(
    "MODEL_MANAGEMENT_ALLOW_UNLOAD", "false"
).lower() in {"1", "true", "yes"}
pipeline_config_path = Path(
    os.getenv("PIPELINE_CONFIG", "/app/config/pipeline.yaml")
)
pipeline_schema_path = Path(
    os.getenv("PIPELINE_SCHEMA", "/app/contracts/pipeline.schema.json")
)
zones_config_path = Path(
    os.getenv("ZONES_CONFIG", "/app/config/zones.json")
)
model_repository_path = Path(
    os.getenv("TRITON_MODEL_REPOSITORY", "/app/models")
)


@app.get("/healthz", tags=["system"])
async def healthz() -> dict[str, str]:
    """Return the liveness state without requiring external dependencies."""
    return {"status": "ok"}


@app.get("/readyz", tags=["system"])
async def readyz() -> dict[str, str]:
    """Verify that PostgreSQL is reachable."""
    try:
        with psycopg.connect(database_url) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="database unavailable"
        ) from error
    return {"status": "ready"}


def load_pipeline_schema() -> dict[str, Any]:
    try:
        return json.loads(
            pipeline_schema_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="pipeline schema unavailable"
        ) from error


def validate_pipeline_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise HTTPException(
            status_code=422, detail="pipeline document must be an object"
        )
    errors = sorted(
        Draft202012Validator(load_pipeline_schema()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(
            str(part) for part in error.absolute_path
        ) or "$"
        raise HTTPException(
            status_code=422,
            detail=f"{location}: {error.message}",
        )

    try:
        zones = json.loads(
            zones_config_path.read_text(encoding="utf-8")
        ).get("cameras", {})
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="zone configuration unavailable"
        ) from error
    pipeline = document["pipeline"]
    cameras = {camera["id"] for camera in pipeline["cameras"]}
    if len(cameras) != len(pipeline["cameras"]):
        raise HTTPException(
            status_code=422, detail="pipeline.cameras: duplicate camera id"
        )
    gpu_ids = {
        pipeline["detection"]["gpu"],
        pipeline["tracker"]["gpu"],
        *(camera["gpu"] for camera in pipeline["cameras"]),
    }
    if len(gpu_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail="all pipeline components must use the same GPU",
        )
    for rule in pipeline.get("rules", []):
        if rule["camera"] not in cameras:
            raise HTTPException(
                status_code=422,
                detail=f"rule references unknown camera {rule['camera']}",
            )
        if rule["type"] == "zone" and (
            rule["zone"]
            not in zones.get(rule["camera"], {}).get("zones", {})
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"rule references unknown zone "
                    f"{rule['camera']}/{rule['zone']}"
                ),
            )

    references = [
        (
            pipeline["detection"]["model"],
            pipeline["detection"]["version"],
        ),
        *[
            (enrichment["model"], None)
            for enrichment in pipeline.get("enrichments", [])
        ],
    ]
    for model, version in references:
        model_path = model_repository_path / model
        if not (model_path / "config.pbtxt").is_file():
            raise HTTPException(
                status_code=422,
                detail=f"model {model} is not present in Triton",
            )
        if version is not None and not (
            model_path / str(version)
        ).is_dir():
            raise HTTPException(
                status_code=422,
                detail=f"model {model} version {version} is not present",
            )
    return document


def read_active_pipeline() -> tuple[str, dict[str, Any]]:
    try:
        raw = pipeline_config_path.read_text(encoding="utf-8")
        document = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as error:
        raise HTTPException(
            status_code=503, detail="active pipeline configuration unavailable"
        ) from error
    return raw, validate_pipeline_document(document)


def serialize_pipeline(raw: str, document: dict[str, Any]) -> dict[str, Any]:
    pipeline = document["pipeline"]
    return {
        "api_version": document["api_version"],
        "name": pipeline["name"],
        "source_sha256": sha256(raw.encode()).hexdigest(),
        "restart_required_for_changes": True,
        "pipeline": pipeline,
    }


@app.get("/v1/pipelines/active", tags=["pipelines"])
def get_active_pipeline() -> dict[str, Any]:
    """Return the validated, secret-free declarative pipeline document."""
    raw, document = read_active_pipeline()
    return serialize_pipeline(raw, document)


@app.get("/v1/pipelines/options", tags=["pipelines"])
def get_pipeline_options() -> dict[str, Any]:
    """Return model and zone choices supported by the workflow editor."""
    try:
        zones = json.loads(
            zones_config_path.read_text(encoding="utf-8")
        ).get("cameras", {})
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="pipeline options unavailable"
        ) from error
    models = sorted(
        path.name
        for path in model_repository_path.iterdir()
        if path.is_dir() and (path / "config.pbtxt").is_file()
    )
    _, active = read_active_pipeline()
    pipeline = active["pipeline"]
    detection_models = {pipeline["detection"]["model"]}
    enrichment_models = {
        item["model"] for item in pipeline.get("enrichments", [])
    }
    return {
        "models": models,
        "detection_models": sorted(detection_models & set(models)),
        "enrichment_models": sorted(enrichment_models & set(models)),
        "zones": {
            camera: sorted(config.get("zones", {}))
            for camera, config in zones.items()
        },
    }


@app.post("/v1/pipelines/validate", tags=["pipelines"])
def validate_pipeline(
    document: dict[str, Any],
    remote_role: str | None = Header(default=None, alias="Remote-Role"),
) -> dict[str, Any]:
    require_admin(remote_role)
    validate_pipeline_document(document)
    return {"valid": True, "restart_required": True}


@app.put("/v1/pipelines/active", tags=["pipelines"])
def update_active_pipeline(
    document: dict[str, Any],
    if_match: str | None = Header(default=None, alias="If-Match"),
    remote_role: str | None = Header(default=None, alias="Remote-Role"),
) -> dict[str, Any]:
    require_admin(remote_role)
    current_raw, _ = read_active_pipeline()
    current_sha = sha256(current_raw.encode()).hexdigest()
    if if_match != current_sha:
        raise HTTPException(
            status_code=409,
            detail="pipeline changed; reload before saving",
        )
    validated = validate_pipeline_document(document)
    rendered = yaml.safe_dump(
        validated,
        sort_keys=False,
        allow_unicode=True,
    )
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=pipeline_config_path.parent,
            prefix=".pipeline-",
            suffix=".yaml",
            delete=False,
        ) as temporary:
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, pipeline_config_path)
    except OSError as error:
        if "temporary_path" in locals():
            temporary_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503, detail="could not persist pipeline"
        ) from error
    return serialize_pipeline(rendered, validated)


def serialize_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "event",
        "id": str(row["id"]),
        "event_type": row["event_type"],
        "object_id": row["object_id"],
        "camera_id": row["camera_id"],
        "track_id": row["track_id"],
        "timestamp": row["occurred_at"].timestamp(),
        "source_update_type": row["source_update_type"],
        "severity": row["severity"],
        "data": row["data"],
    }


@app.get("/v1/events", tags=["events"])
def list_events(
    camera_id: str | None = None,
    event_type: str | None = None,
    before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, list[dict[str, Any]]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if camera_id is not None:
        clauses.append("camera_id = %s")
        parameters.append(camera_id)
    if event_type is not None:
        clauses.append("event_type = %s")
        parameters.append(event_type)
    if before is not None:
        clauses.append("occurred_at < %s")
        parameters.append(before)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(limit)
    with psycopg.connect(
        database_url, row_factory=dict_row
    ) as connection:
        rows = connection.execute(
            f"""
            SELECT id, event_type, object_id, camera_id, track_id,
                   occurred_at, source_update_type, severity, data
            FROM events
            {where}
            ORDER BY occurred_at DESC
            LIMIT %s
            """,
            parameters,
        ).fetchall()
    return {"items": [serialize_event(row) for row in rows]}


@app.get("/v1/events/{event_id}", tags=["events"])
def get_event(event_id: UUID) -> dict[str, Any]:
    with psycopg.connect(
        database_url, row_factory=dict_row
    ) as connection:
        row = connection.execute(
            """
            SELECT id, event_type, object_id, camera_id, track_id,
                   occurred_at, source_update_type, severity, data
            FROM events
            WHERE id = %s
            """,
            (event_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    return serialize_event(row)


def qdrant_request(
    path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    request = Request(
        f"{qdrant_url}/collections/{quote(qdrant_collection, safe='')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read())


def frigate_request(path: str) -> Any:
    with urlopen(f"{frigate_api_url}{path}", timeout=5) as response:
        return json.loads(response.read())


def load_frigate_events(event_ids: list[str]) -> list[dict[str, Any]]:
    """Resolve Frigate Events without overflowing nginx's request line."""
    events: list[dict[str, Any]] = []
    for start in range(0, len(event_ids), 40):
        chunk = event_ids[start : start + 40]
        query = urlencode({"ids": ",".join(chunk)})
        loaded = frigate_request(f"/event_ids?{query}")
        if isinstance(loaded, list):
            events.extend(loaded)
    return events


def existing_frigate_event_ids(event_ids: list[str]) -> set[str]:
    store_url = os.getenv("FRIGATE_EVENT_STORE_URL", "").strip()
    if not store_url or not event_ids:
        return set(event_ids)
    with psycopg.connect(store_url) as connection:
        rows = connection.execute(
            "SELECT id FROM event WHERE id = ANY(%s)",
            (event_ids,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def current_frigate_object_ids(exclude: str | None = None) -> list[str]:
    with psycopg.connect(database_url) as connection:
        links = connection.execute(
            """
            SELECT object_id, frigate_event_id
            FROM frigate_event_links
            WHERE frigate_event_id IS NOT NULL
            """
        ).fetchall()
    present = existing_frigate_event_ids(
        [str(row[1]) for row in links]
    )
    return sorted(
        {
            str(row[0])
            for row in links
            if str(row[1]) in present and str(row[0]) != exclude
        }
    )


def triton_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(
        f"{triton_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=5) as response:
        content = response.read()
    return json.loads(content) if content else {}


def model_ready(name: str) -> bool:
    try:
        triton_request(
            "GET", f"/v2/models/{quote(name, safe='')}/ready"
        )
        return True
    except (HTTPError, TimeoutError, URLError):
        return False


def require_admin(remote_role: str | None) -> None:
    if remote_role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")


@app.get("/v1/heatmap/{camera}.jpg", tags=["analytics"])
def get_heatmap(
    camera: str,
    weight: str = Query("count", pattern="^(count|dwell)$"),
    zones: bool = True,
    start: float | None = Query(None, description="epoch ms; por defecto -24 h"),
    end: float | None = Query(None, description="epoch ms; por defecto ahora"),
) -> Response:
    """Mapa de calor espacial, ya compuesto sobre el snapshot de la cámara.

    Se sirve como imagen y no como datos porque **Grafana no tiene panel de
    heatmap espacial**: el suyo es tiempo x bucket. Se embebe con un panel de
    texto en HTML, que interpola `$__from`/`$__to` y hace que la imagen siga el
    rango del dashboard.
    """
    store_url = os.getenv("FRIGATE_EVENT_STORE_URL", "").strip()
    if not store_url:
        raise HTTPException(
            status_code=503, detail="FRIGATE_EVENT_STORE_URL not configured"
        )
    now_ms = time.time() * 1000
    end_s = (now_ms if end is None else end) / 1000.0
    start_s = ((now_ms - 86_400_000) if start is None else start) / 1000.0
    if start_s >= end_s:
        raise HTTPException(status_code=400, detail="start must be before end")
    try:
        payload = heatmap_render.render(
            store_url, frigate_api_url, zones_config_path,
            camera, start_s, end_s, weight, zones,
        )
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="frigate event store unavailable"
        ) from error
    return Response(
        content=payload,
        media_type="image/jpeg",
        headers={"Cache-Control": f"max-age={int(heatmap_render.CACHE_TTL_S)}"},
    )


@app.get("/v1/models", tags=["models"])
def list_models() -> dict[str, Any]:
    try:
        repository = triton_request("POST", "/v2/repository/index")
    except (HTTPError, TimeoutError, URLError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="Triton unavailable"
        ) from error

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in repository:
        grouped.setdefault(str(entry["name"]), []).append(entry)

    models: list[dict[str, Any]] = []
    for name, entries in sorted(grouped.items()):
        ready = model_ready(name)
        config: dict[str, Any] = {}
        stats: dict[str, Any] = {}
        if ready:
            try:
                config = triton_request(
                    "GET", f"/v2/models/{quote(name, safe='')}/config"
                )
                response = triton_request(
                    "GET", f"/v2/models/{quote(name, safe='')}/stats"
                )
                stats = (response.get("model_stats") or [{}])[0]
            except (HTTPError, TimeoutError, URLError, ValueError):
                pass
        inference_stats = stats.get("inference_stats", {})
        success = inference_stats.get("success", {})
        compute = inference_stats.get("compute_infer", {})
        compute_count = int(compute.get("count", 0))
        models.append(
            {
                "name": name,
                "versions": sorted(
                    {
                        str(entry["version"])
                        for entry in entries
                        if entry.get("version")
                    }
                ),
                "state": "READY"
                if ready
                else str(entries[0].get("state", "UNLOADED")),
                "reason": next(
                    (
                        str(entry["reason"])
                        for entry in entries
                        if entry.get("reason")
                    ),
                    None,
                ),
                "required": name in required_models,
                "platform": config.get("platform"),
                "backend": config.get("backend"),
                "max_batch_size": config.get("max_batch_size"),
                "inputs": config.get("input", []),
                "outputs": config.get("output", []),
                "gpu_ids": sorted(
                    {
                        int(gpu)
                        for group in config.get("instance_group", [])
                        for gpu in group.get("gpus", [])
                    }
                ),
                "dynamic_batching": config.get("dynamic_batching"),
                "inference_count": int(stats.get("inference_count", 0)),
                "execution_count": int(stats.get("execution_count", 0)),
                "success_count": int(success.get("count", 0)),
                "failure_count": int(
                    inference_stats.get("fail", {}).get("count", 0)
                ),
                "average_inference_ms": (
                    float(compute.get("ns", 0))
                    / compute_count
                    / 1_000_000
                    if compute_count
                    else None
                ),
                "last_inference": (
                    float(stats["last_inference"]) / 1000
                    if stats.get("last_inference")
                    else None
                ),
                "can_unload": allow_model_unload
                and name not in required_models,
            }
        )
    return {
        "triton_url": triton_url,
        "allow_unload": allow_model_unload,
        "items": models,
    }


def ensure_known_model(name: str) -> None:
    try:
        repository = triton_request("POST", "/v2/repository/index")
    except (HTTPError, TimeoutError, URLError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="Triton unavailable"
        ) from error
    if name not in {str(entry["name"]) for entry in repository}:
        raise HTTPException(status_code=404, detail="model not found")


@app.post("/v1/models/{name}/load", tags=["models"])
def load_model(
    name: str, remote_role: str | None = Header(None, alias="Remote-Role")
) -> dict[str, str]:
    require_admin(remote_role)
    ensure_known_model(name)
    try:
        triton_request(
            "POST",
            f"/v2/repository/models/{quote(name, safe='')}/load",
        )
    except (HTTPError, TimeoutError, URLError, ValueError) as error:
        raise HTTPException(
            status_code=502, detail="Triton model load failed"
        ) from error
    return {"status": "loaded", "name": name}


@app.post("/v1/models/{name}/unload", tags=["models"])
def unload_model(
    name: str, remote_role: str | None = Header(None, alias="Remote-Role")
) -> dict[str, str]:
    require_admin(remote_role)
    ensure_known_model(name)
    if name in required_models:
        raise HTTPException(
            status_code=409, detail="required model cannot be unloaded"
        )
    if not allow_model_unload:
        raise HTTPException(
            status_code=403, detail="model unload is disabled"
        )
    try:
        triton_request(
            "POST",
            f"/v2/repository/models/{quote(name, safe='')}/unload",
        )
    except (HTTPError, TimeoutError, URLError, ValueError) as error:
        raise HTTPException(
            status_code=502, detail="Triton model unload failed"
        ) from error
    return {"status": "unloaded", "name": name}


def load_embedding_points(
    object_id: str, with_vector: bool = False
) -> list[dict[str, Any]]:
    try:
        return qdrant_request(
            "/points/scroll",
            {
                "limit": 32,
                "with_payload": True,
                "with_vector": with_vector,
                "filter": {
                    "must": [
                        {
                            "key": "object_id",
                            "match": {"value": object_id},
                        }
                    ]
                },
            },
        )["result"]["points"]
    except (KeyError, TimeoutError, URLError, ValueError):
        return []


def load_embeddings(object_id: str) -> list[dict[str, Any]]:
    return [
        {"vector_id": str(point["id"]), **point.get("payload", {})}
        for point in load_embedding_points(object_id)
    ]


@app.get("/v1/objects/{object_id}", tags=["objects"])
def get_object(object_id: str) -> dict[str, Any]:
    with psycopg.connect(
        database_url, row_factory=dict_row
    ) as connection:
        rows = connection.execute(
            """
            SELECT id, event_type, object_id, camera_id, track_id,
                   occurred_at, source_update_type, severity, data
            FROM events
            WHERE object_id = %s
            ORDER BY occurred_at ASC
            """,
            (object_id,),
        ).fetchall()
        frigate_link = connection.execute(
            """
            SELECT frigate_event_id
            FROM frigate_event_links
            WHERE object_id = %s AND frigate_event_id IS NOT NULL
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (object_id,),
        ).fetchone()
    if not rows:
        raise HTTPException(status_code=404, detail="object not found")

    events = [serialize_event(row) for row in rows]
    zones = sorted(
        {
            str(event["data"]["zone"])
            for event in events
            if event["data"].get("zone")
        }
    )
    label = next(
        (
            str(event["data"]["label"])
            for event in events
            if event["data"].get("label")
        ),
        "object",
    )
    return {
        "object_id": object_id,
        "frigate_event_id": (
            str(frigate_link["frigate_event_id"]) if frigate_link else None
        ),
        "camera_id": events[0]["camera_id"],
        "track_id": events[0]["track_id"],
        "label": label,
        "first_seen": events[0]["timestamp"],
        "last_seen": events[-1]["timestamp"],
        "zones": zones,
        "events": events,
        "embeddings": load_embeddings(object_id),
    }


def search_qdrant_similar(
    vector: Any,
    *,
    label: str,
    exclude_object_id: str,
    limit: int,
    offset: int = 0,
    min_score: float = 0,
    restrict_object_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    must: list[dict[str, Any]] = [
        {"key": "label", "match": {"value": label}},
    ]
    if restrict_object_ids:
        must.append(
            {
                "key": "object_id",
                "match": {"any": restrict_object_ids},
            }
        )
    return qdrant_request(
        "/points/search",
        {
            "vector": vector,
            "limit": limit,
            "offset": offset,
            "score_threshold": min_score,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": must,
                "must_not": [
                    {
                        "key": "object_id",
                        "match": {"value": exclude_object_id},
                    }
                ],
            },
        },
    )["result"]


@app.get("/v1/objects/{object_id}/similar", tags=["objects"])
def get_similar_objects(
    object_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    min_score: float = Query(default=0, ge=-1, le=1),
) -> dict[str, Any]:
    source_points = load_embedding_points(object_id, with_vector=True)
    if not source_points:
        raise HTTPException(
            status_code=404, detail="object embedding not found"
        )
    source = max(
        source_points,
        key=lambda point: float(
            point.get("payload", {}).get("frame_timestamp", 0)
        ),
    )
    source_payload = source.get("payload", {})
    try:
        candidates = search_qdrant_similar(
            source["vector"],
            label=str(source_payload.get("label", "car")),
            exclude_object_id=object_id,
            limit=limit,
            offset=offset,
            min_score=min_score,
        )
    except (KeyError, TimeoutError, URLError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="vector search unavailable"
        ) from error

    candidate_object_ids = [
        str(candidate.get("payload", {}).get("object_id", ""))
        for candidate in candidates
        if candidate.get("payload", {}).get("object_id")
    ]
    event_object_ids: set[str] = set()
    if candidate_object_ids:
        with psycopg.connect(database_url) as connection:
            event_object_ids = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT object_id
                    FROM events
                    WHERE object_id = ANY(%s)
                    """,
                    (candidate_object_ids,),
                ).fetchall()
            }

    return {
        "source": {
            "object_id": object_id,
            "vector_id": str(source["id"]),
            **source_payload,
        },
        "metric": "Cosine",
        "threshold_validated": False,
        "items": [
            {
                "object_id": candidate.get("payload", {}).get(
                    "object_id"
                ),
                "vector_id": str(candidate["id"]),
                "score": float(candidate["score"]),
                "has_events": candidate.get("payload", {}).get(
                    "object_id"
                )
                in event_object_ids,
                **candidate.get("payload", {}),
            }
            for candidate in candidates
        ],
    }


@app.get("/v1/frigate-events/{frigate_event_id}/similar", tags=["objects"])
def get_similar_frigate_events(
    frigate_event_id: str,
    limit: int = Query(default=25, ge=1, le=25),
    offset: int = Query(default=0, ge=0),
    min_score: float = Query(default=0, ge=-1, le=1),
) -> list[dict[str, Any]]:
    with psycopg.connect(
        database_url, row_factory=dict_row
    ) as connection:
        source_link = connection.execute(
            """
            SELECT object_id
            FROM frigate_event_links
            WHERE frigate_event_id = %s
            """,
            (frigate_event_id,),
        ).fetchone()
    if source_link is None:
        raise HTTPException(
            status_code=404, detail="not a DeepFrigate tracked object"
        )

    object_id = str(source_link["object_id"])
    source_points = load_embedding_points(object_id, with_vector=True)
    if not source_points:
        return []
    source = max(
        source_points,
        key=lambda point: float(
            point.get("payload", {}).get("frame_timestamp", 0)
        ),
    )
    current_objects = current_frigate_object_ids(exclude=object_id)
    if not current_objects:
        return []
    try:
        candidates = search_qdrant_similar(
            source["vector"],
            label=str(source.get("payload", {}).get("label", "person")),
            exclude_object_id=object_id,
            limit=limit,
            offset=offset,
            min_score=min_score,
            restrict_object_ids=current_objects,
        )
    except (KeyError, TimeoutError, URLError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="vector search unavailable"
        ) from error
    try:
        hydrated = hydrate_similar_frigate_events(
            [
                {
                    "object_id": candidate.get("payload", {}).get("object_id"),
                    "score": float(candidate["score"]),
                    "has_events": True,
                    **candidate.get("payload", {}),
                }
                for candidate in candidates
            ]
        )
    except (HTTPError, TimeoutError, URLError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="Frigate event lookup unavailable"
        ) from error
    next_offset = offset + len(candidates)
    for event in hydrated:
        event["deepfrigate_next_offset"] = next_offset
    return hydrated[:limit]


def hydrate_similar_frigate_events(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scores = {
        str(candidate["object_id"]): float(candidate["score"])
        for candidate in candidates
        if candidate.get("object_id") and candidate.get("has_events")
    }
    if not scores:
        return []

    with psycopg.connect(
        database_url, row_factory=dict_row
    ) as connection:
        links = connection.execute(
            """
            SELECT object_id, frigate_event_id
            FROM frigate_event_links
            WHERE object_id = ANY(%s)
              AND frigate_event_id IS NOT NULL
            ORDER BY object_id, started_at DESC
            """,
            (list(scores),),
        ).fetchall()
    event_to_object = {
        str(link["frigate_event_id"]): str(link["object_id"])
        for link in links
    }
    if not event_to_object:
        return []

    present_ids = existing_frigate_event_ids(list(event_to_object))
    event_to_object = {
        event_id: object_id
        for event_id, object_id in event_to_object.items()
        if event_id in present_ids
    }
    if not event_to_object:
        return []

    frigate_events = load_frigate_events(list(event_to_object))
    hydrated: list[dict[str, Any]] = []
    for event in frigate_events:
        event_id = str(event.get("id", ""))
        object_id = event_to_object.get(event_id)
        if object_id is None:
            continue
        score = max(-1.0, min(1.0, scores[object_id]))
        data = dict(event.get("data") or {})
        event_score = float(
            event.get("score") or data.get("score") or 0
        )
        data.update(
            {
                "type": "object",
                "score": event_score,
                "top_score": float(
                    event.get("top_score")
                    or data.get("top_score")
                    or event_score
                ),
                "region": event.get("region") or data.get("region") or [],
                "box": event.get("box") or data.get("box") or [],
                "area": event.get("area") or data.get("area") or 0,
                "ratio": event.get("ratio") or data.get("ratio") or 1,
            }
        )
        hydrated.append(
            {
                **event,
                "id": event_id,
                "score": event_score,
                "top_score": data["top_score"],
                "zones": event.get("zones") or [],
                "has_snapshot": bool(event.get("has_snapshot")),
                "has_clip": bool(event.get("has_clip")),
                "search_source": "thumbnail",
                "search_distance": 1 - score,
                "data": data,
                "deepfrigate_object_id": object_id,
                "deepfrigate_similarity": score,
            }
        )
    score_order = {object_id: index for index, object_id in enumerate(scores)}
    return sorted(
        hydrated,
        key=lambda event: score_order.get(
            str(event["deepfrigate_object_id"]), len(scores)
        ),
    )
