from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_session_factory(database_url: str):
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    if database_url.endswith(":memory:"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(database_url)
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_database(database_url: str):
    """Create or upgrade a local/test schema. Cloud schema is Alembic-managed."""
    session_factory = create_session_factory(database_url)
    engine = session_factory.kw["bind"]
    Base.metadata.create_all(engine)
    _upgrade_sqlite_columns(engine)
    return session_factory


def _upgrade_sqlite_columns(engine) -> None:
    """Add only forward-compatible columns introduced after the first release."""
    if engine.dialect.name != "sqlite":
        return
    table_names = set(inspect(engine).get_table_names())
    upgrades = {
        "distribution_items": {
            "ai_content_json": "TEXT NOT NULL DEFAULT ''",
            "ai_content_status": "VARCHAR(20) NOT NULL DEFAULT '基础稿'",
            "ai_content_error": "TEXT NOT NULL DEFAULT ''",
            "job_version": "INTEGER NOT NULL DEFAULT 1",
            "template_version": "VARCHAR(30) NOT NULL DEFAULT 'finjob-v1'",
            "row_version": "INTEGER NOT NULL DEFAULT 1",
            "updated_by_user_id": "INTEGER",
            "is_manually_edited": "BOOLEAN NOT NULL DEFAULT 0",
            "published_by_user_id": "INTEGER",
            "published_at": "DATETIME",
            "published_url": "VARCHAR(500) NOT NULL DEFAULT ''",
        },
        "ai_provider_settings": {
            "base_url": "VARCHAR(500) NOT NULL DEFAULT ''",
            "api_mode": "VARCHAR(30) NOT NULL DEFAULT 'chat_completions'",
            "is_active_text_provider": "BOOLEAN NOT NULL DEFAULT 0",
            "row_version": "INTEGER NOT NULL DEFAULT 1",
        },
        "jobs": {
            "is_demo": "BOOLEAN NOT NULL DEFAULT 1",
            "announcement_title": "VARCHAR(200) NOT NULL DEFAULT ''",
            "evidence_status": "VARCHAR(30) NOT NULL DEFAULT '正文已提取'",
            "evidence_note": "TEXT NOT NULL DEFAULT ''",
            "published_at": "DATETIME",
            "collected_at": "DATETIME",
            "content_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''",
            "last_verified_at": "DATETIME",
            "lifecycle_status": "VARCHAR(30) NOT NULL DEFAULT '正常'",
            "last_change_summary": "TEXT NOT NULL DEFAULT ''",
            "notice_type": "VARCHAR(30) NOT NULL DEFAULT '待判断'",
            "notice_type_suggestion": "VARCHAR(30) NOT NULL DEFAULT '待判断'",
            "posting_scope": "VARCHAR(30) NOT NULL DEFAULT 'single_role'",
            "attachment_status": "VARCHAR(30) NOT NULL DEFAULT 'not_required'",
            "application_method": "VARCHAR(30) NOT NULL DEFAULT 'official_page'",
            "application_contact": "VARCHAR(500) NOT NULL DEFAULT ''",
            "student_fit_level": "VARCHAR(30) NOT NULL DEFAULT '待人工判断'",
            "distribution_recommendation": "VARCHAR(30) NOT NULL DEFAULT '仅保留资料库'",
            "ai_rationale": "TEXT NOT NULL DEFAULT ''",
            "ai_confidence": "VARCHAR(10) NOT NULL DEFAULT '低'",
            "intake_grade": "VARCHAR(1) NOT NULL DEFAULT 'C'",
            "intake_route": "VARCHAR(30) NOT NULL DEFAULT '人工复核'",
            "intake_reason": "TEXT NOT NULL DEFAULT ''",
            "intake_evidence": "VARCHAR(160) NOT NULL DEFAULT ''",
            "intake_confidence": "VARCHAR(10) NOT NULL DEFAULT '低'",
            "attachment_links": "TEXT NOT NULL DEFAULT '[]'",
            "parent_job_id": "INTEGER",
            "ai_suggested_score": "INTEGER NOT NULL DEFAULT 0",
            "ai_score_status": "VARCHAR(30) NOT NULL DEFAULT '待建议'",
            "ai_score_reason": "TEXT NOT NULL DEFAULT ''",
            "ai_score_breakdown": "TEXT NOT NULL DEFAULT '{}'",
            "ai_score_confidence": "VARCHAR(10) NOT NULL DEFAULT '低'",
            "ai_scored_at": "DATETIME",
            "verification_checks": "TEXT NOT NULL DEFAULT '{}'",
            "verification_version": "INTEGER NOT NULL DEFAULT 0",
            "row_version": "INTEGER NOT NULL DEFAULT 1",
            "updated_by_user_id": "INTEGER",
        },
        "review_logs": {
            "actor_user_id": "INTEGER",
        },
        "sources": {
            "source_key": "VARCHAR(80)",
            "adapter_key": "VARCHAR(60) NOT NULL DEFAULT ''",
            "scope_group": "VARCHAR(80) NOT NULL DEFAULT ''",
            "is_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "last_success_at": "DATETIME",
            "next_due_at": "DATETIME",
            "consecutive_failure_count": "INTEGER NOT NULL DEFAULT 0",
            "last_error_summary": "TEXT NOT NULL DEFAULT ''",
            "pause_reason": "TEXT NOT NULL DEFAULT ''",
            "library_tier": "VARCHAR(1) NOT NULL DEFAULT 'D'",
            "student_value_score": "INTEGER NOT NULL DEFAULT 0",
            "adaptation_status": "VARCHAR(30) NOT NULL DEFAULT '观察中'",
            "next_action": "VARCHAR(160) NOT NULL DEFAULT '等待人工复查'",
            "official_career_url": "VARCHAR(500) NOT NULL DEFAULT ''",
            "last_monitor_summary": "TEXT NOT NULL DEFAULT '等待首次检查'",
            "validation_state": "VARCHAR(30) NOT NULL DEFAULT '未验证'",
            "adapter_version": "VARCHAR(40) NOT NULL DEFAULT '1'",
            "validated_adapter_version": "VARCHAR(40) NOT NULL DEFAULT ''",
            "validated_rule_version": "VARCHAR(40) NOT NULL DEFAULT ''",
            "validated_at": "DATETIME",
            "validated_by": "VARCHAR(80) NOT NULL DEFAULT ''",
            "next_probe_at": "DATETIME",
        },
    }
    with engine.begin() as connection:
        for table_name, missing_columns in upgrades.items():
            if table_name not in table_names:
                continue
            existing_columns = {
                column["name"] for column in inspect(engine).get_columns(table_name)
            }
            for column_name, column_definition in missing_columns.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
                    )
        if "sources" in table_names:
            connection.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ux_sources_source_key ON sources(source_key)")
            )
        if "source_trial_runs" in table_names:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_source_trial_running_source "
                    "ON source_trial_runs(source_id) WHERE state = 'running'"
                )
            )
