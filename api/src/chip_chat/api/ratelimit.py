"""Per-source-address rate limiting, because sessions are free to mint.

The session caps in :mod:`chip_chat.api.ledger` bound what one conversation can
spend, and a script that wants more simply asks for a new session. The address a
request arrives from is the cheapest thing an abuser cannot re-roll for free, so
it gets its own ceiling underneath the others.

A sliding window rather than a fixed one: a fixed window lets a caller spend its
whole allowance in the last second of one window and the whole of the next in
the first second of the following, which is twice the rate the number promised.
"""

import threading
from collections import deque

from chip_chat.api.clock import Clock, SystemClock
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import BudgetScope, Stop, StopReason, Usage

__all__ = ["SourceRateLimiter"]

_SWEEP_THRESHOLD = 4096
"""Tracked addresses above which empty windows are swept."""


class SourceRateLimiter:
    """Counts recent requests per source address and refuses the excess.

    Thread-safe, and process-local for the same reason the ledger is: one
    instance, one counter, one place for a shared store to land later.
    """

    __slots__ = ("_clock", "_limits", "_lock", "_windows")

    def __init__(
        self,
        limits: SpendLimits | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Initialise an empty limiter.

        Args:
            limits: Supplies the window length and the request ceiling.
            clock: Source of monotonic time. Defaults to the system clock.
        """
        self._limits = limits if limits is not None else SpendLimits()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    def check(self, source_address: str) -> Stop | None:
        """Admit one request from ``source_address``, or refuse it.

        Only admitted requests are recorded. A caller that keeps hammering
        after being refused therefore leaves the window as it was and becomes
        welcome again once it drains -- refusals that extended the window would
        make the limit a ban.

        Args:
            source_address: The client address, already resolved from whatever
                proxy headers the deployment trusts. Any stable string works.

        Returns:
            A :class:`~chip_chat.api.outcome.Stop` when the address is over its
            ceiling, otherwise ``None``.
        """
        limit = self._limits.source_requests_per_window
        with self._lock:
            now = self._clock.monotonic()
            cutoff = now - self._limits.source_window_seconds
            self._sweep_locked(cutoff)
            window = self._windows.setdefault(source_address, deque())
            while window and window[0] <= cutoff:
                window.popleft()
            usage = Usage(scope=BudgetScope.SOURCE_ADDRESS, used=len(window), limit=limit)
            if len(window) >= limit:
                return Stop(reason=StopReason.SOURCE_RATE_LIMIT, usage=usage)
            window.append(now)
            return None

    def usage(self, source_address: str) -> Usage:
        """How many requests ``source_address`` has made inside the window."""
        with self._lock:
            cutoff = self._clock.monotonic() - self._limits.source_window_seconds
            window = self._windows.get(source_address, deque())
            recent = sum(1 for stamp in window if stamp > cutoff)
            return Usage(
                scope=BudgetScope.SOURCE_ADDRESS,
                used=recent,
                limit=self._limits.source_requests_per_window,
            )

    def _sweep_locked(self, cutoff: float) -> None:
        """Forget addresses whose windows have entirely drained.

        Only once the dictionary is large enough to be worth the pass, so a
        normal request pays nothing for it.
        """
        if len(self._windows) <= _SWEEP_THRESHOLD:
            return
        self._windows = {
            address: window
            for address, window in self._windows.items()
            if window and window[-1] > cutoff
        }
