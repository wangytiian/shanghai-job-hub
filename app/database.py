from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_database(database_url: str):
    if database_url.endswith(":memory:"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    _upgrade_sqlite_columns(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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
        },
        "ai_provider_settings": {
            "base_url": "VARCHAR(500) NOT NULL DEFAULT ''",
            "api_mode": "VARCHAR(30) NOT NULL DEFAULT 'chat_completions'",
            "is_active_text_provider": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "jobs": {
            "is_demo": "BOOLEAN NOT NULL DEFAULT 1",
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
        },
        "sources": {
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
