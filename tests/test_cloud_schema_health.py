from sqlalchemy import text

from app.database import create_database
from app.services.health import cloud_schema_is_ready


def test_cloud_schema_health_requires_alembic_version_table_and_head_revision():
    factory = create_database("sqlite+pysqlite:///:memory:")
    with factory() as session:
        assert cloud_schema_is_ready(session) is False
        session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('old')"))
        session.commit()
        assert cloud_schema_is_ready(session) is False
        session.execute(text("UPDATE alembic_version SET version_num = '0002_source_diagnostics'"))
        session.commit()
        assert cloud_schema_is_ready(session) is True
