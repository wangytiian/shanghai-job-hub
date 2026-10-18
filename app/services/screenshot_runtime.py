"""One local background thread, restricted to screenshot work only."""

from dataclasses import dataclass
import threading

from app.services.screenshot_tasks import SCREENSHOT_KINDS, build_screenshot_handlers
from app.services.work_queue import recover_expired_leases
from app.worker import run_once


@dataclass
class ScreenshotExecutor:
    thread: threading.Thread
    stop_event: threading.Event
    last_error_code: str = ""


def start_screenshot_executor(*, session_factory, settings, ai_service, poll_seconds=1.0):
    if settings.app_env != "local" or not settings.screenshot_executor_enabled:
        return None
    stop = threading.Event()
    handlers = build_screenshot_handlers(session_factory, ai_service, settings.screenshot_upload_root)
    executor = ScreenshotExecutor(None, stop)

    def run():
        while not stop.is_set():
            try:
                with session_factory() as session:
                    recover_expired_leases(session, allowed_kinds=SCREENSHOT_KINDS)
                if stop.is_set():
                    break
                worked = run_once(session_factory, allowed_kinds=SCREENSHOT_KINDS, handlers=handlers)
                executor.last_error_code = ""
            except Exception:
                # UI can expose this safe state; no request bodies or keys logged.
                executor.last_error_code = "executor_error"
                worked = False
            if not worked:
                stop.wait(poll_seconds)

    executor.thread = threading.Thread(target=run, daemon=True, name="screenshot-executor")
    executor.thread.start()
    return executor


def stop_screenshot_executor(executor):
    if executor is not None:
        executor.stop_event.set()
        # Stop claiming immediately. An in-flight request retains its heartbeat
        # and finishes safely, or is recovered from its lease after termination.
        executor.thread.join(timeout=2)
