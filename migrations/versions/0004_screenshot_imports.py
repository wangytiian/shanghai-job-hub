"""Persist screenshot evidence and the confirmed content used by both channels."""

from alembic import op
import sqlalchemy as sa

from app.models import ScreenshotImport, ScreenshotAsset, ScreenshotPiece

revision = "0004_screenshot_imports"
down_revision = "0003_source_trials"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in (ScreenshotImport.__table__, ScreenshotAsset.__table__, ScreenshotPiece.__table__):
        table.create(bind=bind, checkfirst=True)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("jobs")}
    additions = (
        sa.Column("role_details_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("role_details_evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed_content_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed_content_version", sa.Integer(), nullable=False, server_default="0"),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("jobs", column)
    bind.execute(sa.text("UPDATE work_items SET status='needs_review', error_code='duplicate_active_key_migration' WHERE id IN (SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY dedupe_key ORDER BY id) AS n FROM work_items WHERE status IN ('queued','running')) AS ranked WHERE n>1)"))
    if "uq_work_items_active_dedupe" not in {index["name"] for index in sa.inspect(bind).get_indexes("work_items")}:
        op.create_index("uq_work_items_active_dedupe", "work_items", ["dedupe_key"], unique=True,
                        sqlite_where=sa.text("status IN ('queued','running')"),
                        postgresql_where=sa.text("status IN ('queued','running')"))


def downgrade():
    raise RuntimeError("Screenshot evidence is retained; restore a verified backup for rollback")
