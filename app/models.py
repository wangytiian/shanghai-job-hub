from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    source_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    scope_group: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    library_tier: Mapped[str] = mapped_column(String(1), default="D", nullable=False)
    student_value_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    adaptation_status: Mapped[str] = mapped_column(String(30), default="观察中", nullable=False)
    next_action: Mapped[str] = mapped_column(String(160), default="等待人工复查", nullable=False)
    official_career_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    last_monitor_summary: Mapped[str] = mapped_column(Text, default="等待首次检查", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="正常", nullable=False)
    check_frequency_hours: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    pause_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    validation_state: Mapped[str] = mapped_column(String(30), default="未验证", nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    validated_adapter_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    validated_rule_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validated_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SourceDiagnostic(Base):
    __tablename__ = "source_diagnostics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    depth: Mapped[str] = mapped_column(String(20), nullable=False, default="connection")
    connection_status: Mapped[str] = mapped_column(String(30), nullable=False)
    content_status: Mapped[str] = mapped_column(String(40), nullable=False)
    adapter_status: Mapped[str] = mapped_column(String(30), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detail_success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)


class SourceTrialRun(Base):
    __tablename__ = "source_trial_runs"
    __table_args__ = (
        UniqueConstraint("source_id", "idempotency_key", name="uq_source_trial_idempotency"),
        Index(
            "ux_source_trial_running_source",
            "source_id",
            unique=True,
            sqlite_where=text("state = 'running'"),
            postgresql_where=text("state = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    adapter_version: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="running")
    budget_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    list_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exclusion_summary: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_code: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_by: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)


class SourceTrialSample(Base):
    __tablename__ = "source_trial_samples"
    __table_args__ = (
        UniqueConstraint("run_id", "identity_key", name="uq_source_trial_sample_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("source_trial_runs.run_id"), nullable=False, index=True
    )
    identity_key: Mapped[str] = mapped_column(String(300), nullable=False)
    announcement_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    job_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    employer_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    location_category: Mapped[str] = mapped_column(String(30), nullable=False, default="原文未明确")
    location_detail: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    published_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    deadline: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    application_kind: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    application_value: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    application_evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    reason_codes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    employer_name: Mapped[str] = mapped_column(String(160), nullable=False)
    announcement_title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    job_title: Mapped[str] = mapped_column(String(160), nullable=False)
    job_family: Mapped[str] = mapped_column(String(80), nullable=False)
    recruitment_type: Mapped[str] = mapped_column(String(30), nullable=False)
    location_category: Mapped[str] = mapped_column(String(30), nullable=False)
    location_detail: Mapped[str] = mapped_column(String(160), nullable=False)
    target_audience: Mapped[str] = mapped_column(String(60), nullable=False)
    direction_tags: Mapped[str] = mapped_column(String(200), nullable=False)
    deadline: Mapped[str] = mapped_column(String(40), nullable=False)
    official_url: Mapped[str] = mapped_column(String(500), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(30), default="正文已提取", nullable=False)
    evidence_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    ai_suggested_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_score_status: Mapped[str] = mapped_column(String(30), default="待建议", nullable=False)
    ai_score_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ai_score_breakdown: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    ai_score_confidence: Mapped[str] = mapped_column(String(10), default="低", nullable=False)
    ai_scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    risk_flags: Mapped[str] = mapped_column(Text, default="演示数据，不代表真实招聘", nullable=False)
    is_demo: Mapped[bool] = mapped_column(default=True, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(30), default="正常", nullable=False)
    last_change_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="待审核", nullable=False)
    notice_type: Mapped[str] = mapped_column(String(30), default="待判断", nullable=False)
    notice_type_suggestion: Mapped[str] = mapped_column(String(30), default="待判断", nullable=False)
    posting_scope: Mapped[str] = mapped_column(String(30), default="single_role", nullable=False)
    attachment_status: Mapped[str] = mapped_column(String(30), default="not_required", nullable=False)
    application_method: Mapped[str] = mapped_column(String(30), default="official_page", nullable=False)
    application_contact: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    student_fit_level: Mapped[str] = mapped_column(String(30), default="待人工判断", nullable=False)
    distribution_recommendation: Mapped[str] = mapped_column(String(30), default="仅保留资料库", nullable=False)
    ai_rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ai_confidence: Mapped[str] = mapped_column(String(10), default="低", nullable=False)
    verification_checks: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    verification_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    intake_grade: Mapped[str] = mapped_column(String(1), default="C", nullable=False)
    intake_route: Mapped[str] = mapped_column(String(30), default="人工复核", nullable=False)
    intake_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    intake_evidence: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    intake_confidence: Mapped[str] = mapped_column(String(10), default="低", nullable=False)
    attachment_links: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )


class ReviewLog(Base):
    __tablename__ = "review_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    operator_name: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class DistributionItem(Base):
    __tablename__ = "distribution_items"
    __table_args__ = (UniqueConstraint("job_id", "channel", name="uq_distribution_job_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    audience_group: Mapped[str] = mapped_column(String(60), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ai_content_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ai_content_status: Mapped[str] = mapped_column(String(20), default="基础稿", nullable=False)
    ai_content_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    job_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    template_version: Mapped[str] = mapped_column(String(30), default="finjob-v1", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="待发送", nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_manually_edited: Mapped[bool] = mapped_column(default=False, nullable=False)
    published_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class AiProviderSetting(Base):
    __tablename__ = "ai_provider_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    api_mode: Mapped[str] = mapped_column(String(30), default="chat_completions", nullable=False)
    text_model: Mapped[str] = mapped_column(String(80), nullable=False)
    ocr_model: Mapped[str] = mapped_column(String(80), nullable=False)
    text_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_active_text_provider: Mapped[bool] = mapped_column(default=False, nullable=False)
    ocr_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    key_masked: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    connection_status: Mapped[str] = mapped_column(String(30), default="not_configured", nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="member", nullable=False)
    can_review: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(default=True, nullable=False)
    activation_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    changes: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class WorkItem(Base):
    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("task_runs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_row_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_revision: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    template_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_token: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_code: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
