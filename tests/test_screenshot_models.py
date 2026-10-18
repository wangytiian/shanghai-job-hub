from sqlalchemy import inspect, select

from app.database import create_database
import app.models as models


def test_screenshot_tables_and_fact_snapshot_columns_exist():
    factory = create_database("sqlite+pysqlite:///:memory:")
    schema = inspect(factory.kw["bind"])
    assert {"screenshot_imports", "screenshot_assets", "screenshot_pieces"} <= set(schema.get_table_names())
    assert {"role_details_status", "role_details_evidence", "confirmed_content_json", "confirmed_content_version"} <= {c["name"] for c in schema.get_columns("jobs")}


def test_import_persists_without_a_job_and_piece_cache_is_not_global_unique():
    assert hasattr(models, "ScreenshotImport"), "missing persistent screenshot work area"
    factory = create_database("sqlite+pysqlite:///:memory:")
    with factory() as session:
        for number in (1, 2):
            batch = models.ScreenshotImport(submission_key=f"submit-{number}")
            session.add(batch)
            session.flush()
            asset = models.ScreenshotAsset(import_id=batch.id, position=0, storage_key=f"image-{number}.png", pixel_sha256="a" * 64)
            session.add(asset)
            session.flush()
            session.add(models.ScreenshotPiece(asset_id=asset.id, piece_index=0, storage_key=f"piece-{number}.png", cache_key="same-cache"))
        session.commit()
        assert len(session.scalars(select(models.ScreenshotImport)).all()) == 2
        assert len(session.scalars(select(models.ScreenshotPiece)).all()) == 2
