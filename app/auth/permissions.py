"""Server-side permission policy for the single shared workspace."""

from app.models import User


class PermissionDenied(PermissionError):
    pass


MEMBER_PERMISSIONS = {
    "business.read",
    "business.edit",
    "task.submit",
    "distribution.preview",
}
REVIEWER_PERMISSIONS = MEMBER_PERMISSIONS | {"review.decide", "distribution.publish"}
ALL_PERMISSIONS = REVIEWER_PERMISSIONS | {
    "settings.manage",
    "accounts.manage",
    "security.read",
    "sources.approve",
}


def require_permission(user: User, action: str) -> None:
    if not user.is_active:
        raise PermissionDenied("账号已停用")
    if user.role == "admin":
        return
    allowed = REVIEWER_PERMISSIONS if user.can_review else MEMBER_PERMISSIONS
    if action not in allowed:
        raise PermissionDenied("当前账号没有此操作权限")
