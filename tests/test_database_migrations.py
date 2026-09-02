import sqlite3

from app.database import create_database


def test_existing_sqlite_jobs_table_is_upgraded_without_rebuilding(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, fingerprint VARCHAR(300) NOT NULL UNIQUE)"
    )
    connection.execute("INSERT INTO jobs (fingerprint) VALUES ('existing-record')")
    connection.commit()
    connection.close()

    create_database(f"sqlite:///{database_path.as_posix()}")

    connection = sqlite3.connect(database_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    saved = connection.execute("SELECT fingerprint FROM jobs").fetchone()[0]
    connection.close()

    assert {"is_demo", "collected_at"}.issubset(columns)
    assert saved == "existing-record"


def test_database_adds_source_library_columns_without_resetting_sources(tmp_path):
    database_path = tmp_path / "legacy-sources.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE, url VARCHAR(500) NOT NULL, level VARCHAR(10) NOT NULL, source_type VARCHAR(30) NOT NULL)"
    )
    connection.execute(
        "INSERT INTO sources (name, url, level, source_type) VALUES ('旧来源', 'https://example.com', '一级', '企业官网')"
    )
    connection.commit()
    connection.close()

    create_database(f"sqlite:///{database_path.as_posix()}")

    connection = sqlite3.connect(database_path)
    source_columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
    saved = connection.execute("SELECT name FROM sources").fetchone()[0]
    connection.close()

    assert {
        "library_tier",
        "student_value_score",
        "adaptation_status",
        "next_action",
        "official_career_url",
        "last_monitor_summary",
    }.issubset(source_columns)
    assert saved == "旧来源"


def test_database_creates_safe_ai_provider_settings_table_without_api_key(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, fingerprint VARCHAR(300) NOT NULL UNIQUE)"
    )
    connection.execute("INSERT INTO jobs (fingerprint) VALUES ('existing-record')")
    connection.commit()
    connection.close()

    create_database(f"sqlite:///{database_path.as_posix()}")

    connection = sqlite3.connect(database_path)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(ai_provider_settings)")
    }
    saved = connection.execute("SELECT fingerprint FROM jobs").fetchone()[0]
    connection.close()

    assert {
        "provider",
        "text_model",
        "ocr_model",
        "text_enabled",
        "ocr_enabled",
        "key_masked",
        "connection_status",
        "last_tested_at",
        "last_error_summary",
        "base_url",
        "is_active_text_provider",
    }.issubset(columns)
    assert "api_key" not in columns
    assert saved == "existing-record"


def test_database_adds_collection_health_and_verification_columns_without_resetting_jobs(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, fingerprint VARCHAR(300) NOT NULL UNIQUE)"
    )
    connection.execute("INSERT INTO jobs (fingerprint) VALUES ('existing-record')")
    connection.commit()
    connection.close()

    create_database(f"sqlite:///{database_path.as_posix()}")

    connection = sqlite3.connect(database_path)
    source_columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
    job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    saved = connection.execute("SELECT fingerprint FROM jobs").fetchone()[0]
    connection.close()

    assert {
        "last_success_at",
        "next_due_at",
        "consecutive_failure_count",
        "last_error_summary",
        "pause_reason",
    }.issubset(source_columns)
    assert {
        "content_fingerprint",
        "last_verified_at",
        "lifecycle_status",
        "last_change_summary",
    }.issubset(job_columns)
    assert saved == "existing-record"
