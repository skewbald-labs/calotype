"""Rate limit for self-serve key creation.

PRIVACY: this limiter holds client IP addresses in process memory only. The
app calls ``sweep`` every ``SWEEP_INTERVAL_S`` seconds, which forgets any
address with no attempt in the last ``window_s - SWEEP_INTERVAL_S`` seconds,
so an address is held for less than one window (an hour by default) after
its last attempt. Addresses are never written to the database, a file or the
logs, and they are gone when the process restarts. Nothing else in Calotype
stores an IP address. The privacy page (site/pages/privacy.html) says this.
"""

from __future__ import annotations

import threading
import time
from collections import deque

# How often the app sweeps the limiters (see ``SlidingWindowLimiter.sweep``).
SWEEP_INTERVAL_S = 60.0


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float = 3600.0, max_tracked: int = 100_000, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self.max_tracked = max_tracked
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, bucket: str) -> tuple[bool, int]:
        """Record one attempt for ``bucket`` if allowed.

        Returns (allowed, seconds until the next attempt would be allowed).
        A limit of 0 or less allows nothing: every attempt is refused, with a
        whole window to wait, and no bucket is held for it.
        """
        if self.limit <= 0:
            return False, max(int(self.window_s), 1)
        now = self._clock()
        with self._lock:
            if len(self._hits) >= self.max_tracked:
                self._prune(now)
            hits = self._hits.get(bucket)
            if hits is None:
                hits = self._hits[bucket] = deque()
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = int(self.window_s - (now - hits[0])) + 1
                return False, max(retry, 1)
            hits.append(now)
            return True, 0

    def sweep(self, margin_s: float = SWEEP_INTERVAL_S) -> int:
        """Forget every bucket with no attempt in the last ``window_s - margin_s`` seconds.

        Run every ``margin_s`` seconds, this keeps a bucket (an IP address) for
        less than ``window_s`` after its last attempt. A bucket dropped up to
        ``margin_s`` early only frees its last attempts that much sooner.
        Returns how many buckets were dropped.
        """
        cutoff = self._clock() - max(self.window_s - margin_s, 0.0)
        with self._lock:
            stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
            for key in stale:
                del self._hits[key]
        return len(stale)

    def tracked(self) -> int:
        """How many buckets are held now."""
        with self._lock:
            return len(self._hits)

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window_s]:
            del self._hits[key]
        # Still full of live entries: drop the oldest half rather than grow without bound.
        if len(self._hits) >= self.max_tracked:
            for key in sorted(self._hits, key=lambda k: self._hits[k][-1])[: self.max_tracked // 2]:
                del self._hits[key]


def client_ip(peer: str | None, forwarded_for: str | None, trusted_hops: int) -> str:
    """The client's address, for in-memory rate limiting only.

    With ``trusted_hops`` = 0 the socket peer is used. Behind N trusted
    proxies (Cloud Run's front end is 1), the Nth address from the right of
    X-Forwarded-For is used; entries further left can be forged by the client.
    """
    if trusted_hops > 0 and forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= trusted_hops:
            return parts[-trusted_hops]
    return peer or "unknown"
