"""Shared resilience primitives for retries and circuit breaking."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a circuit breaker is open and requests are blocked."""


class CircuitBreaker:
    """Async circuit breaker with closed/open/half-open states."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple[type[BaseException], ...] = (Exception,),
        name: str = "default",
        on_state_change: Callable[[str], None] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be > 0")

        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions
        self.on_state_change = on_state_change

        self._state = "closed"
        self._failure_count = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    async def execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Execute an async operation under circuit-breaker protection."""
        await self._before_call()

        try:
            result = await operation()
        except self.expected_exceptions:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state != "open":
                return

            if self._opened_at is None:
                self._opened_at = time.monotonic()

            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.recovery_timeout:
                self._set_state("half_open")
                return

            raise CircuitBreakerOpenError(
                f"Circuit '{self.name}' is open; retry after {self.recovery_timeout - elapsed:.2f}s"
            )

    async def _on_failure(self) -> None:
        async with self._lock:
            if self._state == "half_open":
                self._failure_count = max(1, self._failure_count)
                self._trip_open()
                return

            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._trip_open()

    async def _on_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            self._opened_at = None
            if self._state != "closed":
                self._set_state("closed")

    def _trip_open(self) -> None:
        self._opened_at = time.monotonic()
        self._set_state("open")

    def _set_state(self, new_state: str) -> None:
        if self._state == new_state:
            return
        self._state = new_state
        if self.on_state_change:
            self.on_state_change(new_state)


def retry_with_backoff(
    *,
    max_retries: int = 3,
    base_delay_s: float = 0.5,
    max_delay_s: float = 5.0,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[BaseException, int, float], None] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator for retrying async calls with exponential backoff.

    ``max_retries`` counts retries after the initial attempt.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    if base_delay_s < 0 or max_delay_s < 0:
        raise ValueError("delay values must be >= 0")

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except retry_exceptions as exc:
                    if attempt >= max_retries:
                        raise

                    delay = min(base_delay_s * (2**attempt), max_delay_s)
                    retry_number = attempt + 1
                    if on_retry:
                        on_retry(exc, retry_number, delay)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    attempt += 1

        return wrapper

    return decorator
