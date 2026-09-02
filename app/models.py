from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
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


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    employer_name: Mapped[str] = mapped_column(String(160), nullable=False)
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
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_flags: Mapped[str] = mapped_column(Text, default="演示数据，不代表真实招聘", nullable=False)
    is_demo: Mapped[bool] = mapped_column(default=True, nullable=False)
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
    intake_grade: Mapped[str] = mapped_column(String(1), default="C", nullable=False)
    intake_route: Mapped[str] = mapped_column(String(30), default="人工复核", nullable=False)
    intake_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    intake_evidence: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    intake_confidence: Mapped[str] = mapped_column(String(10), default="低", nullable=False)
    attachment_links: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
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
    status: Mapped[str] = mapped_column(String(20), default="待发送", nullable=False)
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
