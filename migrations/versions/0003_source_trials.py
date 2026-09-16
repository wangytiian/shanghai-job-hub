"""Add persistent B-source trial runs, samples, and validation metadata."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0003_source_trials"
down_revision = "0002_source_diagnostics"
branch_labels = None
depends_on = None


SOURCE_COLUMNS = (
    ("source_key", sa.String(length=80), True, None),
    ("validation_state", sa.String(length=30), False, "未验证"),
    ("adapter_version", sa.String(length=40), False, "1"),
    ("validated_adapter_version", sa.String(length=40), False, ""),
    ("validated_rule_version", sa.String(length=40), False, ""),
    ("validated_at", sa.DateTime(), True, None),
    ("validated_by", sa.String(length=80), False, ""),
    ("next_probe_at", sa.DateTime(), True, None),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    source_columns = {column["name"] for column in inspector.get_columns("sources")}
    for name, column_type, nullable, default in SOURCE_COLUMNS:
        if name not in source_columns:
            op.add_column(
                "sources",
                sa.Column(name, column_type, nullable=nullable, server_default=default),
            )
    inspector = inspect(bind)
    source_indexes = {index["name"] for index in inspector.get_indexes("sources")}
    if "ux_sources_source_key" not in source_indexes:
        op.create_index("ux_sources_source_key", "sources", ["source_key"], unique=True)

    tables = set(inspector.get_table_names())
    if "source_trial_runs" not in tables:
        op.create_table(
            "source_trial_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.String(length=36), nullable=False, unique=True),
            sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
            sa.Column("adapter_version", sa.String(length=40), nullable=False),
            sa.Column("rule_version", sa.String(length=40), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("state", sa.String(length=30), nullable=False, server_default="running"),
            sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("list_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail_attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail_success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("exclusion_summary", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("error_code", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
            sa.Column("requested_by", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("idempotency_key", sa.String(length=120), nullable=False),
            sa.UniqueConstraint("source_id", "idempotency_key", name="uq_source_trial_idempotency"),
        )
        op.create_index("ix_source_trial_runs_source_id", "source_trial_runs", ["source_id"])
    run_indexes = {index["name"] for index in inspect(bind).get_indexes("source_trial_runs")}
    if "ux_source_trial_running_source" not in run_indexes:
        op.create_index(
            "ux_source_trial_running_source",
            "source_trial_runs",
            ["source_id"],
            unique=True,
            sqlite_where=sa.text("state = 'running'"),
            postgresql_where=sa.text("state = 'running'"),
        )
    if "source_trial_samples" not in tables:
        op.create_table(
            "source_trial_samples",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.String(length=36), sa.ForeignKey("source_trial_runs.run_id"), nullable=False),
            sa.Column("identity_key", sa.String(length=300), nullable=False),
            sa.Column("announcement_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("job_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("employer_name", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("title", sa.String(length=240), nullable=False, server_default=""),
            sa.Column("location_category", sa.String(length=30), nullable=False, server_default="原文未明确"),
            sa.Column("location_detail", sa.String(length=240), nullable=False, server_default=""),
            sa.Column("published_at", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("deadline", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("evidence_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("source_url", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("application_kind", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("application_value", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("application_evidence", sa.Text(), nullable=False, server_default=""),
            sa.Column("decision", sa.String(length=30), nullable=False),
            sa.Column("reason_codes", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.UniqueConstraint("run_id", "identity_key", name="uq_source_trial_sample_identity"),
        )
        op.create_index("ix_source_trial_samples_run_id", "source_trial_samples", ["run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "source_trial_samples" in tables:
        op.drop_table("source_trial_samples")
    if "source_trial_runs" in tables:
        op.drop_table("source_trial_runs")
    source_columns = {column["name"] for column in inspect(bind).get_columns("sources")}
    for name, *_ in reversed(SOURCE_COLUMNS):
        if name in source_columns:
            op.drop_column("sources", name)
