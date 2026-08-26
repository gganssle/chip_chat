"""Time, injected rather than imported.

Every wait and every timestamp in this package goes through a :class:`Clock`.
Tests substitute a fake one, which is why the rate limiter can be tested
without a test suite that actually sleeps.
"""

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """The three things this package needs from time."""

    def monotonic(self) -> float:
        """Return a monotonically increasing number of seconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""
        ...

    def now(self) -> datetime:
        """Return the current wall-clock time, timezone-aware and in UTC."""
        ...


class SystemClock:
    """The real clock: :mod:`time` and :func:`datetime.datetime.now`."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def now(self) -> datetime:
        return datetime.now(UTC)
