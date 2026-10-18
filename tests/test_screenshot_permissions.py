import pytest

from app.auth.permissions import PermissionDenied
from app.auth.service import create_user
from app.models import Job
from test_screenshot_confirmation import confirm
from test_screenshot_workflow import call_save, make_import, valid_data, workflow


def test_member_cannot_directly_approve_or_generate(session):
    member = create_user(session, "member", "A secure password", "普通成员")
    record = make_import(session)
    with pytest.raises(PermissionDenied):
        confirm(session, record, actor=member)
    assert session.query(Job).count() == 0


def test_disabled_member_cannot_save(session):
    member = create_user(session, "disabled", "A secure password", "停用成员")
    member.is_active = False
    session.commit()
    record = make_import(session)
    with pytest.raises(PermissionDenied):
        call_save(session, record, {"title": "修改"}, actor=member)


def test_cloud_never_treats_missing_actor_as_local_admin(session, monkeypatch):
    record = make_import(session)
    monkeypatch.setenv("APP_ENV", "cloud")
    with pytest.raises(PermissionDenied):
        call_save(session, record, {"title": "修改"})


def test_reviewer_can_approve_but_false_verification_values_are_rejected(session):
    reviewer = create_user(session, "reviewer", "A secure password", "审核员", can_review=True)
    record = make_import(session)
    checks = valid_data()["verification_checks"]
    checks["source_checked"] = "true"
    with pytest.raises(ValueError):
        confirm(session, record, valid_data(verification_checks=checks), actor=reviewer)
    assert session.query(Job).count() == 0
