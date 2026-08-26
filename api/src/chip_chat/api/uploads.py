"""Upload flooding, which is a cost attack before it is a storage one.

:mod:`chip_chat.api.ratelimit` counts *requests* per address, and that ceiling
is sized for typing: twenty a minute, because a person cannot type faster. An
upload is not typing. One accepted photograph is a Content Safety call, a blob
write with a forty-eight hour retention obligation, and a vision completion --
and a script can issue them as fast as it can open sockets. Twenty of those a
minute is not a person who types quickly, it is an invoice.

So uploads get their own counters, and two of them:

======================= ========================================================
Ceiling                 What it stops
======================= ========================================================
Per session             One conversation uploading in a loop.
Per source address      The same loop with a fresh session per upload, which
                        costs an attacker nothing at all.
======================= ========================================================

Both are sliding windows, for the reason :mod:`chip_chat.api.ratelimit` gives:
a fixed window lets a caller spend its whole allowance at the end of one and the
whole of the next at the start of the following, which is twice the rate the
number promised.

Two properties are worth stating because both are easy to lose in a refactor.

**Nothing is recorded unless everything admits.** The two windows are read and
written under one lock, and an upload refused by the second ceiling does not
leave a mark on the first. Recording a refusal would make the limit a slow ban
rather than a rate.

**Neither refusal says which ceiling it was.** Both carry
:attr:`~chip_chat.api.outcome.StopReason.UPLOAD_RATE_LIMIT` and
:data:`~chip_chat.api.outcome.STOP_STATE_MESSAGE`; the scope goes on the span.
An uploader told "your session is out" learns to mint a new session.

.. code-block:: python

    limiter = UploadLimiter(SpendLimits.from_env())
    stop = limiter.check(session_id=sid, source_address=ip)
    if stop is not None:
        return stop_state_response(stop.message)   # nothing was read
"""

import threading
from collections import deque

from chip_chat.api.clock import Clock, SystemClock
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import BudgetScope, Stop, StopReason, Usage

__all__ = ["UploadLimiter"]

_SWEEP_THRESHOLD = 4096
"""Tracked keys above which drained windows are swept, as in the rate limiter."""


class UploadLimiter:
    """Counts recent uploads per session and per source address, and refuses the excess.

    Thread-safe, and process-local for the same reason the ledger is: one
    instance, one counter, one place for a shared store to land later.
    """

    __slots__ = ("_by_address", "_by_session", "_clock", "_limits", "_lock")

    def __init__(
        self,
        limits: SpendLimits | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Initialise an empty limiter.

        Args:
            limits: Supplies the window length and both ceilings.
            clock: Source of monotonic time. Defaults to the system clock.
        """
        self._limits = limits if limits is not None else SpendLimits()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._lock = threading.Lock()
        self._by_session: dict[str, deque[float]] = {}
        self._by_address: dict[str, deque[float]] = {}

    @property
    def limits(self) -> SpendLimits:
        """The ceilings this limiter enforces."""
        return self._limits

    def check(self, *, session_id: str, source_address: str) -> Stop | None:
        """Admit one upload, or refuse it.

        The address ceiling is evaluated first, matching
        :meth:`~chip_chat.api.guard.SpendGuard._decide`: the layer that an
        attacker cannot re-roll for free goes in front of the one they can.
        Neither window is written unless both admit.

        Args:
            session_id: The conversation the upload belongs to.
            source_address: The client address, already resolved from whatever
                proxy headers the deployment trusts.

        Returns:
            A :class:`~chip_chat.api.outcome.Stop` when either ceiling is
            reached, otherwise ``None``.
        """
        limits = self._limits
        with self._lock:
            now = self._clock.monotonic()
            cutoff = now - limits.upload_window_seconds
            self._sweep_locked(cutoff)

            address = self._window_locked(self._by_address, source_address, cutoff)
            if len(address) >= limits.source_uploads_per_window:
                return _refuse(
                    BudgetScope.SOURCE_UPLOADS,
                    len(address),
                    limits.source_uploads_per_window,
                )

            session = self._window_locked(self._by_session, session_id, cutoff)
            if len(session) >= limits.session_uploads_per_window:
                return _refuse(
                    BudgetScope.SESSION_UPLOADS,
                    len(session),
                    limits.session_uploads_per_window,
                )

            # Both admitted, so both are charged. Doing this last is what keeps
            # a refusal from consuming the other scope's allowance.
            address.append(now)
            session.append(now)
            return None

    def session_usage(self, session_id: str) -> Usage:
        """How many uploads ``session_id`` has made inside the window."""
        return self._usage(
            self._by_session,
            session_id,
            BudgetScope.SESSION_UPLOADS,
            self._limits.session_uploads_per_window,
        )

    def address_usage(self, source_address: str) -> Usage:
        """How many uploads ``source_address`` has made inside the window."""
        return self._usage(
            self._by_address,
            source_address,
            BudgetScope.SOURCE_UPLOADS,
            self._limits.source_uploads_per_window,
        )

    def _usage(
        self,
        windows: dict[str, deque[float]],
        key: str,
        scope: BudgetScope,
        limit: int,
    ) -> Usage:
        """Count one window without mutating it."""
        with self._lock:
            cutoff = self._clock.monotonic() - self._limits.upload_window_seconds
            window = windows.get(key, deque())
            recent = sum(1 for stamp in window if stamp > cutoff)
            return Usage(scope=scope, used=recent, limit=limit)

    def _window_locked(
        self, windows: dict[str, deque[float]], key: str, cutoff: float
    ) -> deque[float]:
        """Return ``key``'s window with everything older than ``cutoff`` dropped."""
        window = windows.setdefault(key, deque())
        while window and window[0] <= cutoff:
            window.popleft()
        return window

    def _sweep_locked(self, cutoff: float) -> None:
        """Forget keys whose windows have entirely drained.

        Only once a dictionary is large enough to be worth the pass, so an
        ordinary upload pays nothing for it.
        """
        for name in ("_by_session", "_by_address"):
            windows: dict[str, deque[float]] = getattr(self, name)
            if len(windows) <= _SWEEP_THRESHOLD:
                continue
            setattr(
                self,
                name,
                {
                    key: window
                    for key, window in windows.items()
                    if window and window[-1] > cutoff
                },
            )


def _refuse(scope: BudgetScope, used: int, limit: int) -> Stop:
    """Build the refusal for one ceiling.

    Both scopes produce the same reason and the same sentence -- only
    :attr:`~chip_chat.api.outcome.Usage.scope` differs, and that is read off the
    span rather than off the response.
    """
    return Stop(
        reason=StopReason.UPLOAD_RATE_LIMIT,
        usage=Usage(scope=scope, used=used, limit=limit),
    )
