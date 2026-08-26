"""Time, injected rather than imported.

Every instant the spend cap reads goes through a :class:`Clock`. Two of the
acceptance criteria on this feature -- that the counters reset correctly at the
day boundary, and that a per-source rate limit survives a loop from one host --
are properties of *when* things happen, and a test that had to wait for real
time to pass would be slower, flakier, and much weaker evidence.
"""

import time
from datetime import UTC, datetime
from typing import Protocol

__all__ = ["Clock", "SystemClock"]


class Clock(Protocol):
    """The two things the spend cap needs from time."""

    def monotonic(self) -> float:
        """Return a monotonically increasing number of seconds.

        Used for the rate-limit window, which must not move when somebody
        adjusts the system clock or a leap second lands.
        """
        ...

    def now(self) -> datetime:
        """Return the current wall-clock time, timezone-aware and in UTC.

        Used for the daily ceiling, which resets on a calendar boundary and so
        genuinely needs wall time.
        """
        ...


class SystemClock:
    """The real clock: :func:`time.monotonic` and :func:`datetime.datetime.now`."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)
