"""Validation and compilation for DeepFrigate declarative pipelines."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
import yaml


# Segundos sin recibir datos antes de forzar una reconexion RTSP. El defecto
# de `nvurisrcbin` es 0 = desactivado, y con eso un EOS de la fuente es
# definitivo: DeepStream la suelta y el pipeline sigue con las demas sin avisar
# a nadie. Paso el 3 sep con la camara `user`, que se cayo a las 20:51 y no
# volvio en 70 minutos mientras `tienda` seguia. No se habia notado nunca
# porque `tienda` es un fichero en bucle servido en local, que no falla.
#
# 10 s: suficiente para no reconectar por un hipo de red, y bastante menos que
# el hueco de 70 min de aquel dia.
DEFAULT_RTSP_RECONNECT_INTERVAL = 10
# -1 es el defecto de nvurisrcbin: reintentar sin limite. Explicito porque una
# camara que vuelve sola a las 3 de la manana es justo lo que se quiere.
DEFAULT_RTSP_RECONNECT_ATTEMPTS = -1


class PipelineConfigError(ValueError):
    """Raised when a declarative pipeline cannot be compiled safely."""


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    document = yaml.safe_load(raw)
    if not isinstance(document, dict):
        raise PipelineConfigError("pipeline document must be an object")
    return document, raw


def _validate_schema(
    document: dict[str, Any], schema_path: Path
) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "$"
    raise PipelineConfigError(f"{location}: {error.message}")


def _validate_references(
    document: dict[str, Any],
    *,
    zones_path: Path | None,
    model_repository_path: Path | None,
) -> None:
    pipeline = document["pipeline"]
    if zones_path is not None:
        zones = json.loads(zones_path.read_text(encoding="utf-8")).get(
            "cameras", {}
        )
        for rule in pipeline.get("rules", []):
            if rule["type"] != "zone":
                continue
            camera_zones = zones.get(rule["camera"], {}).get("zones", {})
            if rule["zone"] not in camera_zones:
                raise PipelineConfigError(
                    f"rule references unknown zone {rule['camera']}/"
                    f"{rule['zone']}"
                )

    if model_repository_path is not None:
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
                raise PipelineConfigError(
                    f"model {model} is not present in the Triton repository"
                )
            if version is not None and not (
                model_path / str(version)
            ).is_dir():
                raise PipelineConfigError(
                    f"model {model} version {version} is not present"
                )


def compile_pipeline(
    document: dict[str, Any],
    *,
    environment: Mapping[str, str],
    source_sha256: str,
) -> dict[str, Any]:
    """Resolve environment-backed sources and enforce cross-field invariants."""
    pipeline = document["pipeline"]
    cameras = pipeline["cameras"]
    camera_ids = [camera["id"] for camera in cameras]
    if len(camera_ids) != len(set(camera_ids)):
        raise PipelineConfigError("pipeline.cameras contains duplicate ids")

    resolved_cameras: list[dict[str, Any]] = []
    for camera in cameras:
        source_env = camera["source_env"]
        uri = environment.get(source_env)
        if not uri:
            raise PipelineConfigError(
                f"camera {camera['id']}: environment variable "
                f"{source_env} is not set"
            )
        reconnect = camera.get(
            "rtsp_reconnect_interval", DEFAULT_RTSP_RECONNECT_INTERVAL
        )
        if not isinstance(reconnect, int) or isinstance(reconnect, bool):
            raise PipelineConfigError(
                f"camera {camera['id']}: rtsp_reconnect_interval must be an "
                "integer number of seconds"
            )
        if reconnect < 0:
            raise PipelineConfigError(
                f"camera {camera['id']}: rtsp_reconnect_interval must be >= 0"
            )
        attempts = camera.get(
            "rtsp_reconnect_attempts", DEFAULT_RTSP_RECONNECT_ATTEMPTS
        )
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise PipelineConfigError(
                f"camera {camera['id']}: rtsp_reconnect_attempts must be an "
                "integer"
            )
        resolved_cameras.append(
            {
                "id": camera["id"],
                "uri": uri,
                "source_env": source_env,
                "gpu": camera.get("gpu", 0),
                "rtsp_reconnect_interval": reconnect,
                "rtsp_reconnect_attempts": attempts,
            }
        )

    detection = {**pipeline["detection"]}
    detection.setdefault("gpu", 0)
    tracker = {**pipeline["tracker"]}
    tracker.setdefault("gpu", 0)
    pipeline_gpu = detection["gpu"]
    gpu_assignments = [
        *(camera["gpu"] for camera in resolved_cameras),
        tracker["gpu"],
    ]
    if any(gpu != pipeline_gpu for gpu in gpu_assignments):
        raise PipelineConfigError(
            "all cameras, detection and tracker must use the same GPU"
        )

    camera_id_set = set(camera_ids)
    for rule in pipeline.get("rules", []):
        if rule["camera"] not in camera_id_set:
            raise PipelineConfigError(
                f"rule references unknown camera {rule['camera']}"
            )

    enrichments = pipeline.get("enrichments", [])
    export_labels = sorted(
        pipeline.get("frame_export", {}).get(
            "labels",
            {
                label
                for enrichment in enrichments
                for label in enrichment["labels"]
            },
        )
    )
    return {
        "api_version": document["api_version"],
        "name": pipeline["name"],
        "source_sha256": source_sha256,
        "cameras": resolved_cameras,
        "detection": detection,
        "tracker": tracker,
        "frame_export": pipeline.get("frame_export"),
        "enrichments": enrichments,
        "export_labels": export_labels,
        "rules": pipeline.get("rules", []),
    }


def load_pipeline(
    path: str | Path,
    *,
    schema_path: str | Path,
    environment: Mapping[str, str] | None = None,
    zones_path: str | Path | None = None,
    model_repository_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load, validate and compile one pipeline document."""
    pipeline_path = Path(path)
    document, raw = _read_document(pipeline_path)
    _validate_schema(document, Path(schema_path))
    _validate_references(
        document,
        zones_path=Path(zones_path) if zones_path is not None else None,
        model_repository_path=(
            Path(model_repository_path)
            if model_repository_path is not None
            else None
        ),
    )
    return compile_pipeline(
        document,
        environment=os.environ if environment is None else environment,
        source_sha256=sha256(raw.encode()).hexdigest(),
    )
