import pytest

from app.keys import overlapping, s3_key


def test_s3_key_keeps_frigate_utc_layout() -> None:
    assert (
        s3_key(
            "tienda-norte-01",
            "/media/frigate/recordings/2026-08-31/16/tienda/02.10.mp4",
        )
        == "tienda-norte-01/recordings/2026-08-31/16/tienda/02.10.mp4"
    )


def test_s3_key_rejects_non_recording_paths() -> None:
    with pytest.raises(ValueError):
        s3_key("local", "/media/frigate/clips/tienda-1.jpg")


def test_s3_key_rejects_path_separators_in_site() -> None:
    with pytest.raises(ValueError):
        s3_key("acme/east", "/media/frigate/recordings/a/b/c/00.00.mp4")


def test_overlapping_matches_frigate_window() -> None:
    assert overlapping(100.0, 120.0, 90.0, 110.0)
    assert overlapping(100.0, 120.0, 110.0, 130.0)
    assert not overlapping(100.0, 120.0, 80.0, 100.0)
    assert not overlapping(100.0, 120.0, 120.0, 130.0)
