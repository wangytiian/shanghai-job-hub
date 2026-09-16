"""Add persisted source diagnostic records without altering existing sources."""

from sqlalchemy import inspect

from alembic import op


revision = "0002_source_diagnostics"
down_revision = "0001_existing_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "source_diagnostics" in inspect(bind).get_table_names():
        return
    op.create_table(
        "source_diagnostics",
        op.Column("id", op.Integer(), primary_key=True),
        op.Column("source_id", op.Integer(), op.ForeignKey("sources.id"), nullable=False),
        op.Column("checked_at", op.DateTime(), nullable=False),
        op.Column("depth", op.String(length=20), nullable=False, server_default="connection"),
        op.Column("connection_status", op.String(length=30), nullable=False),
        op.Column("content_status", op.String(length=40), nullable=False),
        op.Column("adapter_status", op.String(length=30), nullable=False),
        op.Column("http_status", op.Integer(), nullable=True),
        op.Column("final_url", op.String(length=500), nullable=False, server_default=""),
        op.Column("sample_count", op.Integer(), nullable=False, server_default="0"),
        op.Column("detail_success_count", op.Integer(), nullable=False, server_default="0"),
        op.Column("error_code", op.String(length=60), nullable=False, server_default=""),
        op.Column("message", op.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_source_diagnostics_source_id", "source_diagnostics", ["source_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "source_diagnostics" in inspect(bind).get_table_names():
        op.drop_table("source_diagnostics")
