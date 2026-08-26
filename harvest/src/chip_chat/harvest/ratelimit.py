"""Politeness controls: a minimum gap between requests, and a concurrency cap.

Both are process-wide by construction. :data:`GLOBAL_GATE` is the single gate
every harvester shares by default, so adding a fourth harvester in a later
issue cannot triple the request rate a site sees — the ceiling is a property
of the process, not of any one harvester.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from chip_chat.harvest.clock import Clock, SystemClock

DEFAULT_MIN_INTERVAL_SECONDS = 2.0
"""Politely slow. There is no deadline here that justifies anything faster."""

DEFAULT_MAX_CONCURRENCY = 1
"""Serial by default. Raise it only with a reason, and never far."""


class RateLimiter:
    """Spaces requests at least ``min_interval`` seconds apart.

    Slots are reserved under a lock and waited for outside it, so N threads
    calling :meth:`acquire` at once are handed N distinct, evenly spaced
    departure times rather than all sleeping the same interval and then
    stampeding together.
    """

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        """Initialise the limiter.

        Args:
            min_interval: Minimum seconds between the start of one request and
                the next. Must not be negative.
            clock: Source of time and sleeping. Defaults to the system clock.

        Raises:
            ValueError: If ``min_interval`` is negative.
        """
        if min_interval < 0:
            raise ValueError(f"min_interval must not be negative, got {min_interval}")
        self._min_interval = min_interval
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._lock = threading.Lock()
        self._next_slot: float | None = None

    @property
    def min_interval(self) -> float:
        """The current minimum gap between requests, in seconds."""
        return self._min_interval

    def slow_to(self, min_interval: float) -> None:
        """Widen the gap to ``min_interval`` if that is slower than the current one.

        Used to honour a ``Crawl-delay`` in ``robots.txt``, which may ask for
        more patience than our default but is never allowed to ask for less.

        Args:
            min_interval: The requested gap, in seconds.
        """
        with self._lock:
            self._min_interval = max(self._min_interval, min_interval)

    def acquire(self) -> float:
        """Block until the next request may start.

        Returns:
            The number of seconds spent waiting.
        """
        with self._lock:
            now = self._clock.monotonic()
            start_at = now if self._next_slot is None else max(now, self._next_slot)
            self._next_slot = start_at + self._min_interval
            wait = start_at - now
        if wait > 0:
            self._clock.sleep(wait)
        return wait


class PolitenessGate:
    """A rate limiter and a concurrency ceiling, entered as one context.

    The semaphore is taken first so that queued callers wait without holding a
    reserved rate-limit slot, which would otherwise idle the gap they booked.
    """

    def __init__(
        self,
        limiter: RateLimiter | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        """Initialise the gate.

        Args:
            limiter: The shared rate limiter. Defaults to a new one at
                :data:`DEFAULT_MIN_INTERVAL_SECONDS`.
            max_concurrency: How many requests may be in flight at once.

        Raises:
            ValueError: If ``max_concurrency`` is less than one.
        """
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be at least 1, got {max_concurrency}")
        self.limiter = limiter if limiter is not None else RateLimiter()
        self.max_concurrency = max_concurrency
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one concurrency slot, having first waited out the rate limit."""
        with self._semaphore:
            self.limiter.acquire()
            yield


GLOBAL_GATE = PolitenessGate()
"""The process-wide gate. Every harvester shares it unless told otherwise."""
