from pathlib import Path

import pytest

from app.database import create_database
from app.migration_tools import MigrationError, migrate_sqlite
from app.models import Job, Source
from app.seed import seed_demo_data


def _source_database(path: Path):
    factory = create_database(f"sqlite:///{path.as_posix()}")
    with factory() as session:
        seed_demo_data(session)
    return factory


def test_sqlite_migration_dry_run_reports_rows_without_writing_target(tmp_path: Path):
    source_path = tmp_path / "legacy.db"
    _source_database(source_path)
    target_factory = create_database("sqlite+pysqlite:///:memory:")

    result = migrate_sqlite(source_path, target_factory, dry_run=True)

    assert result.dry_run is True
    assert result.row_counts["jobs"] > 0
    with target_factory() as session:
        assert session.query(Job).count() == 0


def test_sqlite_migration_preserves_ids_and_related_job_data(tmp_path: Path):
    source_path = tmp_path / "legacy.db"
    source_factory = _source_database(source_path)
    with source_factory() as session:
        original_job = session.query(Job).first()
        original_source = session.query(Source).first()
    target_factory = create_database("sqlite+pysqlite:///:memory:")

    result = migrate_sqlite(source_path, target_factory, dry_run=False)

    assert result.row_counts["jobs"] > 0
    with target_factory() as session:
        migrated_job = session.get(Job, original_job.id)
        migrated_source = session.get(Source, original_source.id)
        assert migrated_job.fingerprint == original_job.fingerprint
        assert migrated_source.name == original_source.name


def test_sqlite_migration_refuses_nonempty_target(tmp_path: Path):
    source_path = tmp_path / "legacy.db"
    _source_database(source_path)
    target_factory = create_database("sqlite+pysqlite:///:memory:")
    with target_factory() as session:
        session.add(Source(name="已有来源", url="https://example.com", level="A", source_type="官方"))
        session.commit()

    with pytest.raises(MigrationError, match="非空"):
        migrate_sqlite(source_path, target_factory, dry_run=False)
