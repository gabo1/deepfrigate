from copy import deepcopy
from pathlib import Path

import pytest

from app.pipeline_config import PipelineConfigError, load_pipeline


CONFIG = Path("/opt/deepfrigate/config/pipeline.yaml")
SCHEMA = Path("/opt/deepfrigate/contracts/pipeline.schema.json")
ENVIRONMENT = {
    "RTSP_TIENDA": "rtsp://example/tienda",
    "RTSP_USER": "rtsp://example/user",
}


def test_checked_in_pipeline_compiles_deterministically() -> None:
    first = load_pipeline(
        CONFIG, schema_path=SCHEMA, environment=ENVIRONMENT
    )
    second = load_pipeline(
        CONFIG, schema_path=SCHEMA, environment=ENVIRONMENT
    )

    assert first == second
    assert first["name"] == "deepfrigate-multicamera"
    assert [camera["id"] for camera in first["cameras"]] == ["tienda", "user"]
    assert first["detection"]["model"] == "object-detector"
    assert first["tracker"]["type"] == "nvtracker"
    assert first["export_labels"] == ["car", "person"]
    assert len(first["source_sha256"]) == 64


def _document() -> dict:
    import yaml

    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _write(tmp_path: Path, document: dict) -> Path:
    import yaml

    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_missing_source_environment_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PipelineConfigError, match="RTSP_TIENDA is not set"):
        load_pipeline(CONFIG, schema_path=SCHEMA, environment={})


def test_duplicate_camera_ids_are_rejected(tmp_path: Path) -> None:
    document = _document()
    document["pipeline"]["cameras"].append(
        dict(document["pipeline"]["cameras"][0])
    )

    with pytest.raises(PipelineConfigError, match="duplicate ids"):
        load_pipeline(
            _write(tmp_path, document),
            schema_path=SCHEMA,
            environment=ENVIRONMENT,
        )


def test_unknown_rule_camera_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["pipeline"]["rules"] = [
        {"type": "zone", "camera": "unknown", "zone": "area_cajas"}
    ]

    with pytest.raises(PipelineConfigError, match="unknown camera"):
        load_pipeline(
            _write(tmp_path, document),
            schema_path=SCHEMA,
            environment=ENVIRONMENT,
        )


def test_mixed_gpu_pipeline_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["pipeline"]["tracker"]["gpu"] = 1

    with pytest.raises(PipelineConfigError, match="same GPU"):
        load_pipeline(
            _write(tmp_path, document),
            schema_path=SCHEMA,
            environment=ENVIRONMENT,
        )


def test_schema_rejects_unknown_fields(tmp_path: Path) -> None:
    document = deepcopy(_document())
    document["pipeline"]["unexpected"] = True

    with pytest.raises(PipelineConfigError, match="unexpected"):
        load_pipeline(
            _write(tmp_path, document),
            schema_path=SCHEMA,
            environment=ENVIRONMENT,
        )


def test_unknown_zone_reference_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["pipeline"]["rules"] = [
        {"type": "zone", "camera": "tienda", "zone": "area_cajas"}
    ]
    zones = tmp_path / "zones.json"
    zones.write_text(
        '{"cameras":{"tienda":{"zones":{}}}}',
        encoding="utf-8",
    )

    with pytest.raises(PipelineConfigError, match="unknown zone"):
        load_pipeline(
            _write(tmp_path, document),
            schema_path=SCHEMA,
            environment=ENVIRONMENT,
            zones_path=zones,
        )


def test_model_and_version_references_are_validated(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models"
    for model in (
        "object-detector",
        "vehicle-embedding",
        "person-attribute",
        "vehicle-attribute",
    ):
        (models / model).mkdir(parents=True)
        (models / model / "config.pbtxt").write_text(
            f'name: "{model}"', encoding="utf-8"
        )

    with pytest.raises(PipelineConfigError, match="version 1"):
        load_pipeline(
            CONFIG,
            schema_path=SCHEMA,
            environment=ENVIRONMENT,
            model_repository_path=models,
        )

    (models / "object-detector" / "1").mkdir()
    compiled = load_pipeline(
        CONFIG,
        schema_path=SCHEMA,
        environment=ENVIRONMENT,
        model_repository_path=models,
    )
    assert compiled["detection"]["version"] == 1


def test_rtsp_reconnect_defaults_are_applied() -> None:
    """Sin reconexión, un EOS de la fuente es definitivo.

    Es lo que dejó la cámara `user` fuera 70 min el 3 sep mientras `tienda`
    seguía: DeepStream soltó source1 y el pipeline continuó sin avisar.
    """
    config = load_pipeline(CONFIG, schema_path=SCHEMA, environment=ENVIRONMENT)
    for camera in config["cameras"]:
        assert camera["rtsp_reconnect_interval"] == 10
        assert camera["rtsp_reconnect_attempts"] == -1


def test_rtsp_reconnect_can_be_overridden_per_camera(tmp_path: Path) -> None:
    document = _document()
    document["pipeline"]["cameras"][0]["rtsp_reconnect_interval"] = 30
    document["pipeline"]["cameras"][0]["rtsp_reconnect_attempts"] = 5
    config = load_pipeline(
        _write(tmp_path, document), schema_path=SCHEMA,
        environment=ENVIRONMENT,
    )
    assert config["cameras"][0]["rtsp_reconnect_interval"] == 30
    assert config["cameras"][0]["rtsp_reconnect_attempts"] == 5
    assert config["cameras"][1]["rtsp_reconnect_interval"] == 10


def test_rtsp_reconnect_interval_zero_disables_it(tmp_path: Path) -> None:
    document = _document()
    document["pipeline"]["cameras"][0]["rtsp_reconnect_interval"] = 0
    config = load_pipeline(
        _write(tmp_path, document), schema_path=SCHEMA,
        environment=ENVIRONMENT,
    )
    assert config["cameras"][0]["rtsp_reconnect_interval"] == 0


def test_negative_rtsp_reconnect_interval_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["pipeline"]["cameras"][0]["rtsp_reconnect_interval"] = -5
    with pytest.raises(PipelineConfigError):
        load_pipeline(
            _write(tmp_path, document), schema_path=SCHEMA,
            environment=ENVIRONMENT,
        )
