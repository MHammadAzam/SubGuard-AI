"""Thread-safe in-memory sliding-window rate limiter for login and state-changing API routes.

NOTE (Single-Process Pilot Boundary): Counters are kept in process memory, so limits
apply per worker process and reset on server restart. For multi-worker deployments,
back this interface with a shared store such as Redis.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple


class SlidingWindowRateLimiter:
    """Sliding-window counter keyed by arbitrary string identifiers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: Dict[str, Deque[float]] = defaultdict(deque)

    def check_and_record(self, key: str, *, limit: int, window_seconds: int) -> Tuple[bool, int]:
        """Record an event if under limit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            dq = self._buckets[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                retry_after = max(1, int(window_seconds - (now - dq[0])) + 1)
                return False, retry_after
            dq.append(now)
            return True, 0

    def reset(self) -> None:
        """Clear all rate-limit buckets (for deterministic testing)."""
        with self._lock:
            self._buckets.clear()


login_rate_limiter = SlidingWindowRateLimiter()
api_mutation_rate_limiter = SlidingWindowRateLimiter()
