"""Fixed-window rate limiter.

Scope: per process. Each API instance enforces the limit independently, so with
N instances the effective global limit is N x RATE_LIMIT_REQUESTS. That is an
accepted, documented trade-off: rate limiting here protects a single instance
from abuse, it is not a billing control. Correctness-critical coordination
(claims, leases, failure detection) is done in PostgreSQL, never in memory.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.config import settings


@dataclass
class _Window:
    count: int
    started_at: float


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._buckets: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int, int]:
        """Return ``(allowed, remaining, retry_after_seconds)``."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or now - bucket.started_at >= self.window:
                self._buckets[key] = _Window(count=1, started_at=now)
                return True, self.limit - 1, 0

            if bucket.count >= self.limit:
                retry_after = int(self.window - (now - bucket.started_at)) + 1
                return False, 0, retry_after

            bucket.count += 1
            return True, self.limit - bucket.count, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def prune(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [k for k, v in self._buckets.items() if now - v.started_at >= self.window * 2]
            for key in stale:
                self._buckets.pop(key, None)


limiter = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)
