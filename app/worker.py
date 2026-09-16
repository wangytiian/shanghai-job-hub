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
    session_factory: Callable[[], Session], *, lease_seconds: int = 120, max_attempts: int = 3
) -> bool:
    with session_factory() as session:
        item = claim_next(session, lease_seconds=lease_seconds)
        if item is None:
            return False
        handler = HANDLERS.get(item.kind)
        if handler is None:
            needs_review(session, item.id, item.lease_token, "handler_not_registered")
        else:
            try:
                handler(session, item)
            except Exception as exc:
                retry_or_needs_review(
                    session, item.id, item.lease_token, type(exc).__name__.lower(), max_attempts=max_attempts
                )
        return True


def main() -> None:
    settings = load_settings()
    session_factory = create_session_factory(settings.database_url)
    while True:
        with session_factory() as session:
            recover_expired_leases(session)
        if not run_once(session_factory, lease_seconds=settings.task_lease_seconds):
            time.sleep(1)


if __name__ == "__main__":
    main()
