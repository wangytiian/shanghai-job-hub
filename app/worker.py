"""Single-worker entry point for persistent work items.

Handlers are introduced alongside each converted web action. Unknown jobs are
kept visible for human review instead of being silently dropped.
"""

import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import load_settings
from app.database import create_session_factory
from app.services.work_queue import claim_next, needs_review, recover_expired_leases, retry_or_needs_review
from app.services.task_handlers import handle_score_job, handle_source_trial


HANDLERS = {"score_job": handle_score_job, "source_trial": handle_source_trial}


def run_once(
    session_factory: Callable[[], Session], *, lease_seconds: int = 120, max_attempts: int = 3,
    allowed_kinds=None, handlers=None,
) -> bool:
    with session_factory() as session:
        item = claim_next(session, lease_seconds=lease_seconds, allowed_kinds=allowed_kinds)
        if item is None:
            return False
        handler = (HANDLERS if handlers is None else handlers).get(item.kind)
        if handler is None:
            needs_review(session, item.id, item.lease_token, "handler_not_registered")
        else:
            try:
                if item.kind.startswith("screenshot_"):
                    session.expunge(item)
                    session.rollback()
                handler(session, item)
            except Exception as exc:
                retry_or_needs_review(
                    session, item.id, item.lease_token, type(exc).__name__.lower(), max_attempts=max_attempts
                )
        return True


def main() -> None:
    settings = load_settings()
    session_factory = create_session_factory(settings.database_url)
    from app.services.ai_runtime import create_ai_settings_service
    from app.services.screenshot_tasks import build_screenshot_handlers
    handlers = {**HANDLERS, **build_screenshot_handlers(session_factory, create_ai_settings_service(settings), settings.screenshot_upload_root)}
    while True:
        with session_factory() as session:
            recover_expired_leases(session)
        if not run_once(session_factory, lease_seconds=settings.task_lease_seconds, handlers=handlers):
            time.sleep(1)


if __name__ == "__main__":
    main()
