"""Create the initial shared-workspace schema."""

from alembic import op

from app.database import Base
import app.models  # noqa: F401


revision = "0001_existing_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
