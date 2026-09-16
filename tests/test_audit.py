from app.auth.service import create_user
from app.services.audit import record_event


def test_record_event_uses_caller_transaction(session):
    user = create_user(session, "member", "A secure password", "普通成员")

    event = record_event(
        session, user.id, user.display_name, "job.updated", "job", "1", "req-1", {"row_version": 2}
    )
    session.rollback()

    assert session.get(type(event), event.id) is None
