"""Readiness checks that are stricter for the deployed shared environment."""

from sqlalchemy import text
from sqlalchemy.orm import Session


CURRENT_SCHEMA_REVISION = "0002_source_diagnostics"


def cloud_schema_is_ready(session: Session) -> bool:
    """Require Alembic's recorded head before reporting a cloud instance ready."""
    try:
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception:
        return False
    return revision == CURRENT_SCHEMA_REVISION
