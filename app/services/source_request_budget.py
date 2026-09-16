"""Shared wall-clock and pacing guard for a single source trial."""

from time import monotonic, sleep
from typing import Callable

import httpx


def is_transport_timeout(exc: Exception) -> bool:
    """Recognize both Python and the project's real HTTP client timeout types."""
    return isinstance(exc, (TimeoutError, httpx.TimeoutException))


class RequestBudgetController:
    def __init__(self, *, total_seconds: float, request_timeout_seconds: float, interval_seconds: float, clock: Callable[[], float] = monotonic, sleeper: Callable[[float], None] = sleep) -> None:
        self._clock = clock
        self._sleep = sleeper
        self._deadline = clock() + total_seconds
        self._request_timeout = request_timeout_seconds
        self._interval = interval_seconds
        self._last_request_at: float | None = None

    def before_request(self) -> float:
        current = self._clock()
        if self._last_request_at is not None:
            wait = self._interval - (current - self._last_request_at)
            if wait > 0:
                self.wait(wait)
                current = self._clock()
        remaining = self._deadline - current
        if remaining <= 0:
            raise TimeoutError("试采超过总执行预算")
        self._last_request_at = current
        return min(self._request_timeout, remaining)

    def after_request(self) -> None:
        if self._clock() >= self._deadline:
            raise TimeoutError("试采超过总执行预算")

    def wait(self, seconds: float) -> None:
        remaining = self._deadline - self._clock()
        if seconds >= remaining:
            raise TimeoutError("试采等待将超过总执行预算")
        self._sleep(seconds)
