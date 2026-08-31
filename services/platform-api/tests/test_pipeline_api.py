from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from fastapi import HTTPException
import pytest
import yaml

from app import main


SCHEMA = {
    "type": "object",
    "required": ["api_version", "pipeline"],
    "properties": {
        "api_version": {"const": "deepfrigate/v1"},
        "pipeline": {
            "type": "object",
            "required": ["name", "cameras", "detection", "tracker"],
        },
    },
}

DOCUMENT = {
    "api_version": "deepfrigate/v1",
    "pipeline": {
        "name": "test-pipeline",
        "cameras": [
            {"id": "tienda", "source_env": "RTSP_TIENDA", "gpu": 0}
        ],
        "detection": {
            "model": "object-detector",
            "version": 1,
            "config_path": "/config/detector.pbtxt",
            "gpu": 0,
        },
        "tracker": {
            "type": "nvtracker",
            "config_path": "/config/tracker.yml",
            "width": 640,
            "height": 384,
            "gpu": 0,
        },
        "rules": [
            {"type": "zone", "camera": "tienda", "zone": "cajas"}
        ],
    },
}


@pytest.fixture()
def pipeline_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text(
        yaml.safe_dump(DOCUMENT, sort_keys=False), encoding="utf-8"
    )
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    zones = tmp_path / "zones.json"
    zones.write_text(
        json.dumps(
            {
                "cameras": {
                    "tienda": {"zones": {"cajas": [[0, 0], [1, 1]]}}
                }
            }
        ),
        encoding="utf-8",
    )
    models = tmp_path / "models"
    (models / "object-detector" / "1").mkdir(parents=True)
    (models / "object-detector" / "config.pbtxt").write_text(
        'name: "object-detector"', encoding="utf-8"
    )
    monkeypatch.setattr(main, "pipeline_config_path", pipeline)
    monkeypatch.setattr(main, "pipeline_schema_path", schema)
    monkeypatch.setattr(main, "zones_config_path", zones)
    monkeypatch.setattr(main, "model_repository_path", models)
    return pipeline


def test_active_pipeline_is_secret_free_and_versioned(
    pipeline_files: Path,
) -> None:
    result = main.get_active_pipeline()

    assert result["name"] == "test-pipeline"
    assert result["restart_required_for_changes"] is True
    assert result["source_sha256"] == sha256(
        pipeline_files.read_bytes()
    ).hexdigest()
    assert "rtsp://" not in json.dumps(result)


def test_pipeline_write_requires_admin(pipeline_files: Path) -> None:
    with pytest.raises(HTTPException, match="admin role required"):
        main.validate_pipeline(DOCUMENT, remote_role="viewer")


def test_pipeline_write_uses_optimistic_concurrency(
    pipeline_files: Path,
) -> None:
    with pytest.raises(HTTPException) as error:
        main.update_active_pipeline(
            DOCUMENT,
            if_match="stale",
            remote_role="admin",
        )

    assert error.value.status_code == 409


def test_admin_can_validate_and_update(pipeline_files: Path) -> None:
    current_sha = sha256(pipeline_files.read_bytes()).hexdigest()

    assert main.validate_pipeline(
        DOCUMENT, remote_role="admin"
    ) == {"valid": True, "restart_required": True}
    result = main.update_active_pipeline(
        DOCUMENT,
        if_match=current_sha,
        remote_role="admin",
    )

    assert result["name"] == "test-pipeline"
    assert yaml.safe_load(pipeline_files.read_text()) == DOCUMENT


def test_non_zone_rule_does_not_require_zone(pipeline_files: Path) -> None:
    document = deepcopy(DOCUMENT)
    document["pipeline"]["rules"] = [
        {"type": "stationary", "camera": "tienda"}
    ]

    assert main.validate_pipeline(
        document, remote_role="admin"
    ) == {"valid": True, "restart_required": True}
