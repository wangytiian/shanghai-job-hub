"""Append-only, non-secret activity records."""

import json

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record_event(
    session: Session,
    actor_user_id: int | None,
    actor_label: str,
    action: str,
    entity_type: str,
    entity_id: str,
    request_id: str,
    changes: dict[str, object],
) -> AuditEvent:
    event = AuditEvent(
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        request_id=request_id,
        changes=json.dumps(changes, ensure_ascii=False, sort_keys=True),
    )
    session.add(event)
    session.flush()
    return event
