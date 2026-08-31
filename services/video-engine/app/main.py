"""Declarative DeepStream pipeline with asynchronous FrameRef export."""

from __future__ import annotations

import logging
import os

# Service Maker monitors stdin; keep it open in non-interactive Docker runs.
_stdin_pipe_read, _stdin_pipe_write = os.pipe()
if not os.isatty(0):
    os.dup2(_stdin_pipe_read, 0)
os.close(_stdin_pipe_read)

from pyservicemaker import Pipeline, Probe, Receiver

from .exporter import ExportMetadataCollector, FrameExporter
from .pipeline_config import load_pipeline

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("video-engine")


def _positive_float(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_float(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def build_pipeline() -> tuple[Pipeline, FrameExporter]:
    config = load_pipeline(
        os.getenv(
            "PIPELINE_CONFIG",
            "/opt/deepfrigate/config/pipeline.yaml",
        ),
        schema_path=os.getenv(
            "PIPELINE_SCHEMA",
            "/opt/contracts/pipeline.schema.json",
        ),
        zones_path=os.getenv("ZONES_CONFIG", "/opt/config/zones.json"),
        model_repository_path=os.getenv(
            "TRITON_MODEL_REPOSITORY", "/opt/models"
        ),
    )
    cameras = {
        source_id: camera
        for source_id, camera in enumerate(config["cameras"])
    }
    pipeline_gpu = config["detection"]["gpu"]
    pipeline = Pipeline(config["name"])
    for source_id, camera in cameras.items():
        pipeline.add(
            "nvurisrcbin",
            f"source{source_id}",
            {
                "uri": camera["uri"],
                "gpu-id": camera["gpu"],
                "drop-on-latency": True,
                "latency": 100,
            },
        )

    pipeline.add(
        "nvstreammux",
        "streammux",
        {
            "gpu-id": pipeline_gpu,
            "live-source": True,
            "batch-size": len(cameras),
            "batched-push-timeout": 40000,
            "width": 1280,
            "height": 720,
            "enable-padding": True,
            "nvbuf-memory-type": 0,
        },
    )
    pipeline.add(
        "nvinferserver",
        "primary-inference",
        {
            "config-file-path": os.getenv(
                "INFERENCE_CONFIG",
                config["detection"]["config_path"],
            ),
            "batch-size": len(cameras),
            "unique-id": 1,
        },
    )
    pipeline.add(
        "nvtracker",
        "tracker",
        {
            "tracker-width": config["tracker"]["width"],
            "tracker-height": config["tracker"]["height"],
            "gpu-id": config["tracker"]["gpu"],
            "ll-lib-file": (
                "/opt/nvidia/deepstream/deepstream/lib/"
                "libnvds_nvmultiobjecttracker.so"
            ),
            "ll-config-file": config["tracker"]["config_path"],
            "display-tracking-id": False,
        },
    )
    pipeline.add("tee", "output-tee")

    pipeline.add(
        "queue",
        "broker-queue",
        {"max-size-buffers": 8, "max-size-bytes": 0, "max-size-time": 0},
    )
    pipeline.add(
        "nvmsgconv",
        "message-converter",
        {
            "config": "/opt/deepfrigate/config/msgconv_multicamera.txt",
            "payload-type": 0,
            "msg2p-newapi": True,
            "frame-interval": 1,
        },
    )
    pipeline.add(
        "nvmsgbroker",
        "message-broker",
        {
            "proto-lib": (
                "/opt/nvidia/deepstream/deepstream/lib/libnvds_mqtt_proto.so"
            ),
            "conn-str": os.getenv(
                "MQTT_CONNECTION", "mqtt;1883;deepfrigate-video-engine"
            ),
            "topic": os.getenv("DETECTIONS_TOPIC", "deepfrigate/detections"),
            "sync": False,
            "async": False,
            "qos": False,
        },
    )

    pipeline.add(
        "queue",
        "export-queue",
        {
            "max-size-buffers": 2,
            "max-size-bytes": 0,
            "max-size-time": 0,
            "leaky": 2,
        },
    )
    pipeline.add(
        "nvvideoconvert",
        "export-converter",
        {
            "gpu-id": pipeline_gpu,
            "nvbuf-memory-type": 2,
            "contiguous-buffers": True,
        },
    )
    pipeline.add(
        "capsfilter",
        "export-caps",
        {"caps": "video/x-raw(memory:NVMM),format=RGB"},
    )
    pipeline.add(
        "appsink",
        "export-sink",
        {
            "emit-signals": True,
            "sync": False,
            "async": False,
            "max-buffers": 2,
            "drop": True,
        },
    )

    for source_id in cameras:
        pipeline.link(
            (f"source{source_id}", "streammux"),
            ("", "sink_%u"),
        )
    pipeline.link(
        "streammux", "primary-inference", "tracker", "output-tee"
    )
    pipeline.link(
        ("output-tee", "broker-queue"),
        ("src_%u", ""),
    )
    pipeline.link(
        "broker-queue", "message-converter", "message-broker"
    )
    pipeline.link(
        ("output-tee", "export-queue"),
        ("src_%u", ""),
    )
    pipeline.link(
        "export-queue", "export-converter", "export-caps", "export-sink"
    )

    export_labels = set(config["export_labels"])
    env_export_labels = os.getenv("FRAME_EXPORT_LABELS")
    if env_export_labels is not None:
        export_labels = {
            label.strip()
            for label in env_export_labels.split(",")
            if label.strip()
        }
    if not export_labels:
        raise ValueError("FRAME_EXPORT_LABELS must contain at least one label")
    metadata = ExportMetadataCollector(
        {source_id: camera["id"] for source_id, camera in cameras.items()},
        export_labels,
    )
    exporter = FrameExporter(
        metadata,
        os.getenv("FRAME_STORE_URL", "http://frame-store:8080"),
        ttl_seconds=_positive_float("FRAME_TTL_SECONDS", "15"),
        refresh_seconds=_positive_float("FRAME_REFRESH_SECONDS", "5"),
        min_export_seconds=_positive_float("FRAME_MIN_EXPORT_SECONDS", "1"),
        confidence_improvement=_positive_float(
            "FRAME_CONFIDENCE_IMPROVEMENT", "0.05"
        ),
        crop_padding=_nonnegative_float("FRAME_CROP_PADDING", "0.1"),
        snapshot_dir=os.getenv("DS_SNAPSHOT_DIR") or None,
        snapshot_interval=_positive_float("DS_SNAPSHOT_INTERVAL", "0.4"),
    )
    pipeline.attach(
        "export-queue",
        Probe("frame-export-metadata", metadata),
    )
    pipeline.attach(
        "export-sink",
        Receiver("frame-export-receiver", exporter),
        tips="new-sample",
    )
    pipeline.attach(
        "tracker",
        "measure_fps_probe",
        name="pipeline-fps",
    )
    logger.info(
        "Compiled pipeline name=%s cameras=%s detector=%s:%s "
        "tracker=%s enrichments=%s config_sha256=%s",
        config["name"],
        ",".join(camera["id"] for camera in cameras.values()),
        config["detection"]["model"],
        config["detection"]["version"],
        config["tracker"]["type"],
        ",".join(item["model"] for item in config["enrichments"]) or "none",
        config["source_sha256"],
    )
    return pipeline, exporter


def main() -> None:
    pipeline, exporter = build_pipeline()
    logger.info("Starting declarative pipeline with FrameRef export")
    try:
        pipeline.start().wait()
    finally:
        exporter.close()
        logger.info("Video engine stopped")


if __name__ == "__main__":
    main()
