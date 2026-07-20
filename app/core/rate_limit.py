"""A small in-process sliding-window rate limiter.

Used to throttle the public website-widget endpoint, which triggers paid AI
calls. This is per-process: with multiple Gunicorn workers or replicas the
effective limit is (workers × limit), which is an acceptable safety baseline.
A deployment that needs an exact global limit should back this with a shared
store (e.g. Redis) instead.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Request


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._calls_since_sweep = 0

    def allow(self, key: str) -> bool:
        """Record a hit for ``key``; return False if it exceeds the limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            self._maybe_sweep(cutoff)
            dq = self._hits[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                return False
            dq.append(now)
            return True

    def _maybe_sweep(self, cutoff: float) -> None:
        # Periodically drop keys whose entire window has expired, so idle
        # visitors don't leak memory on a public endpoint.
        self._calls_since_sweep += 1
        if self._calls_since_sweep < 1000:
            return
        self._calls_since_sweep = 0
        for k in list(self._hits):
            dq = self._hits[k]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                del self._hits[k]

    def reset(self, key: str) -> None:
        """Forget one key's history.

        Used after a successful sign-in: failed attempts should accumulate, but
        proving you own the account clears the slate, so a legitimate user who
        fumbles their password a few times and then gets it right isn't left
        one attempt away from a 429 for the rest of the window.
        """
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
            self._calls_since_sweep = 0


def client_ip(request: Request) -> str:
    """Best-effort client IP, honouring a proxy's X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
