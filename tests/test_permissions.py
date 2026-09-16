import pytest

from app.auth.permissions import PermissionDenied, require_permission
from app.auth.service import create_user


def test_member_can_read_and_submit_but_cannot_review_or_manage_settings(session):
    member = create_user(session, "member", "A secure password", "普通成员")

    require_permission(member, "business.read")
    require_permission(member, "business.edit")
    require_permission(member, "task.submit")
    require_permission(member, "distribution.preview")
    with pytest.raises(PermissionDenied):
        require_permission(member, "review.decide")
    with pytest.raises(PermissionDenied):
        require_permission(member, "settings.manage")
    with pytest.raises(PermissionDenied):
        require_permission(member, "sources.approve")


def test_reviewer_can_publish_but_not_manage_accounts(session):
    reviewer = create_user(session, "reviewer", "A secure password", "审核成员", can_review=True)

    require_permission(reviewer, "review.decide")
    require_permission(reviewer, "distribution.publish")
    with pytest.raises(PermissionDenied):
        require_permission(reviewer, "accounts.manage")


def test_admin_has_all_declared_permissions(session):
    admin = create_user(session, "admin", "A secure password", "管理员", role="admin")

    for action in (
        "business.read", "business.edit", "task.submit", "review.decide",
        "distribution.preview", "distribution.publish", "settings.manage",
        "accounts.manage", "security.read",
        "sources.approve",
    ):
        require_permission(admin, action)


def test_inactive_user_is_denied(session):
    member = create_user(session, "member", "A secure password", "普通成员")
    member.is_active = False

    with pytest.raises(PermissionDenied):
        require_permission(member, "business.read")
