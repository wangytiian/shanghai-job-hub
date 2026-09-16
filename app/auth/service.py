"""Database-backed user credentials and revocable server-side sessions."""

from datetime import datetime, timedelta
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import AuthSession, User


class AuthenticationError(ValueError):
    pass


_password_hasher = PasswordHasher()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_username(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 80:
        raise ValueError("用户名不能为空且最多 80 个字符")
    return normalized


def create_user(
    session: Session,
    username: str,
    password: str,
    display_name: str,
    *,
    role: str = "member",
    can_review: bool = False,
    must_change_password: bool = True,
    activation_expires_at: datetime | None = None,
) -> User:
    normalized_username = _normalize_username(username)
    if len(password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    if role not in {"admin", "member"}:
        raise ValueError("账号角色无效")
    if session.scalar(select(User).where(User.username == normalized_username)):
        raise ValueError("用户名已存在")
    user = User(
        username=normalized_username,
        display_name=display_name.strip() or normalized_username,
        password_hash=_password_hasher.hash(password),
        role=role,
        can_review=can_review or role == "admin",
        must_change_password=must_change_password,
        activation_expires_at=activation_expires_at,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def authenticate(session: Session, username: str, password: str) -> User:
    user = session.scalar(select(User).where(User.username == _normalize_username(username)))
    if user is None or not user.is_active:
        raise AuthenticationError("账号或密码错误")
    try:
        valid = _password_hasher.verify(user.password_hash, password)
    except VerifyMismatchError as exc:
        raise AuthenticationError("账号或密码错误") from exc
    if not valid:
        raise AuthenticationError("账号或密码错误")
    if user.activation_expires_at and user.activation_expires_at < datetime.now():
        raise AuthenticationError("账号激活已过期")
    return user


def issue_session(
    session: Session,
    user: User,
    *,
    absolute_lifetime: timedelta = timedelta(hours=8),
) -> tuple[str, str]:
    now = datetime.now()
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    session.add(
        AuthSession(
            user_id=user.id,
            token_hash=_token_hash(raw_token),
            csrf_token_hash=_token_hash(csrf_token),
            last_seen_at=now,
            expires_at=now + absolute_lifetime,
        )
    )
    session.commit()
    return raw_token, csrf_token


def resolve_session(
    session: Session,
    raw_token: str,
    *,
    idle_lifetime: timedelta = timedelta(minutes=30),
) -> User:
    now = datetime.now()
    auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(raw_token)))
    if auth_session is None or auth_session.revoked_at is not None or auth_session.expires_at <= now:
        raise AuthenticationError("登录状态已失效")
    if auth_session.last_seen_at + idle_lifetime <= now:
        auth_session.revoked_at = now
        session.commit()
        raise AuthenticationError("登录状态已失效")
    if auth_session.user_id is None:
        raise AuthenticationError("登录状态无效")
    user = session.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("登录状态已失效")
    auth_session.last_seen_at = now
    session.commit()
    return user


def revoke_user_sessions(session: Session, user_id: int) -> None:
    session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now())
    )
    session.commit()


def change_password(session: Session, user: User, new_password: str) -> None:
    if len(new_password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    user.password_hash = _password_hasher.hash(new_password)
    user.must_change_password = False
    session.flush()
    session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now())
    )
    session.commit()


def reset_password(session: Session, user: User, temporary_password: str) -> None:
    """Admin reset: invalidate all existing sessions and require a fresh password."""
    if len(temporary_password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    user.password_hash = _password_hasher.hash(temporary_password)
    user.must_change_password = True
    user.activation_expires_at = datetime.now() + timedelta(hours=24)
    session.flush()
    session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now())
    )
    session.commit()


def csrf_valid(session: Session, raw_token: str, raw_csrf_token: str) -> bool:
    if not raw_token or not raw_csrf_token:
        return False
    auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(raw_token)))
    return bool(
        auth_session
        and auth_session.revoked_at is None
        and hmac.compare_digest(auth_session.csrf_token_hash, _token_hash(raw_csrf_token))
    )
