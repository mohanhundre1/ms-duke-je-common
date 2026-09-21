
"""Tiny in-process TTL cache for resolved secrets.

Thread-safe via a single :class:`threading.Lock`. Values are stored as
``(value, expires_at_monotonic)`` tuples; ``None`` is a legal cached value and
indicates "looked up and confirmed missing" so we don't keep hitting the
backend for known-missing names.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0")
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[str, tuple[Optional[str], float]] = {}

    def get(self, key: str) -> tuple[bool, Optional[str]]:
        """Return ``(hit, value)``. ``hit=False`` means the caller must resolve."""
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if now >= expires_at:
                self._store.pop(key, None)
                return False, None
            return True, value

    def set(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            self._store[key] = (value, time.monotonic() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
