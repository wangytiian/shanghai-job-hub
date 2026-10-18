import sys
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

# Configure before importing application modules: app.main creates a global app.
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SECRET_BACKEND"] = "file"
os.environ["BAILIAN_SECRET_FILE"] = ""
os.environ["OPENAI_COMPATIBLE_SECRET_FILE"] = ""
os.environ["SCREENSHOT_EXECUTOR_ENABLED"] = "0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import create_database
from app.models import Job
from app.seed import seed_demo_data


@pytest.fixture
def session():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    database_session = session_factory()
    try:
        yield database_session
    finally:
        database_session.close()


@pytest.fixture
def demo_job(session):
    seed_demo_data(session)
    return session.query(Job).first()


@pytest.fixture
def pending_review_job(demo_job):
    demo_job.status = "待审核"
    demo_job.deadline = (date.today() + timedelta(days=30)).isoformat()
    return demo_job
