from app.models import Job
from app.seed import seed_demo_data


def test_seed_creates_ten_distinct_demo_jobs(session):
    result = seed_demo_data(session)

    assert result.created_jobs == 10
    assert session.query(Job).count() == 10
    assert len({job.fingerprint for job in session.query(Job).all()}) == 10
