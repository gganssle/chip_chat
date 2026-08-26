"""The running token counter, and the reservation that makes it safe under load.

Reserve, then settle. A turn claims a pessimistic
:attr:`~chip_chat.api.limits.SpendLimits.turn_token_reservation` against the
ceiling *before* the model is called, and replaces it with the real number
afterwards. The alternative -- read the counter, decide, call the model, add
what it cost -- is a check-then-act race: put twenty visitors on one process a
few tokens below the ceiling and all twenty read a number under the limit, all
twenty proceed, and the ceiling is crossed by nineteen turns' worth of spend.
Sequential tests pass either way, which is exactly why RFC-001 asks for the
concurrent one.

The counter is process-local. That is honest for the single-instance deployment
this demo runs on and is the reason :class:`BudgetLedger` keeps its state behind
one lock and one interface: a Redis-backed implementation later has one place to
land.
"""

import threading
from dataclasses import dataclass, field
from datetime import date

from chip_chat.api.clock import Clock, SystemClock
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import BudgetScope, Stop, StopReason, Usage

__all__ = ["BudgetLedger", "Reservation"]

_SWEEP_THRESHOLD = 2048
"""Tracked sessions above which idle ones are swept. See :meth:`BudgetLedger.reserve`."""

_SESSION_IDLE_SECONDS = 3600.0
"""How long a session may go untouched before a sweep may forget it."""


@dataclass(slots=True)
class _SessionState:
    turns: int = 0
    tokens: int = 0
    reserved: int = 0
    last_seen: float = 0.0


@dataclass(slots=True)
class Reservation:
    """One attempt to claim budget for a turn, granted or refused.

    A granted reservation holds tokens against both the global ceiling and the
    session cap until :meth:`settle` replaces them with the real cost. Use it
    through :meth:`chip_chat.api.guard.SpendGuard.turn`, which cannot forget to
    settle; the class is public because the ledger is testable on its own.
    """

    ledger: "BudgetLedger | None"
    """The ledger holding the tokens, or ``None`` for a refusal from another layer."""

    session_id: str
    tokens: int
    usage: Usage
    day: date | None = None
    stop: Stop | None = None
    _settled: bool = field(default=False, repr=False)

    @property
    def granted(self) -> bool:
        """True when the turn may proceed and a model may be called."""
        return self.stop is None

    def settle(self, tokens_used: int) -> None:
        """Replace the reservation with what the turn actually cost.

        Idempotent, and a no-op on a refused reservation, so a caller unwinding
        an error path cannot double-count.

        Args:
            tokens_used: Prompt plus completion tokens across the whole turn.
                Negative values are treated as zero.
        """
        if self._settled or not self.granted or self.ledger is None:
            return
        self._settled = True
        self.ledger.commit(self, max(0, tokens_used))

    def release(self) -> None:
        """Give the reservation back unspent, for a turn that called no model."""
        self.settle(0)


class BudgetLedger:
    """Daily token counters for the whole process and for each session.

    Thread-safe. Every mutation happens under one lock, which is what lets the
    concurrent case be reasoned about at all.
    """

    __slots__ = (
        "_clock",
        "_committed",
        "_day",
        "_limits",
        "_lock",
        "_reserved",
        "_sessions",
    )

    def __init__(
        self, limits: SpendLimits | None = None, clock: Clock | None = None
    ) -> None:
        """Initialise an empty ledger.

        Args:
            limits: The ceilings to enforce. Defaults to :class:`SpendLimits`.
            clock: Source of time. Defaults to the system clock.
        """
        self._limits = limits if limits is not None else SpendLimits()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._lock = threading.Lock()
        self._committed = 0
        self._reserved = 0
        self._sessions: dict[str, _SessionState] = {}
        self._day = self._today()

    @property
    def limits(self) -> SpendLimits:
        """The ceilings this ledger enforces."""
        return self._limits

    def _today(self) -> date:
        """The calendar day the daily ceiling is currently counting."""
        return self._clock.now().astimezone(self._limits.zone()).date()

    def _roll_over_locked(self) -> None:
        """Reset the daily counters if the calendar day has changed."""
        today = self._today()
        if today == self._day:
            return
        self._day = today
        self._committed = 0
        self._reserved = 0
        # Sessions are a property of a day too: "come back tomorrow" is a
        # promise that tomorrow's visitor starts from zero turns.
        self._sessions.clear()

    def _sweep_locked(self, now: float) -> None:
        """Forget sessions nothing has touched for an hour.

        Only runs once the dictionary is large enough to be worth the pass, so
        the common request pays nothing for it. Sessions with tokens still
        reserved are kept -- their settle has not arrived yet.
        """
        if len(self._sessions) <= _SWEEP_THRESHOLD:
            return
        cutoff = now - _SESSION_IDLE_SECONDS
        self._sessions = {
            session_id: state
            for session_id, state in self._sessions.items()
            if state.reserved > 0 or state.last_seen > cutoff
        }

    def global_usage(self) -> Usage:
        """How close the global daily ceiling is, right now.

        Counts committed spend plus everything currently reserved, because a
        turn in flight is spend that has already been promised.
        """
        with self._lock:
            self._roll_over_locked()
            return Usage(
                scope=BudgetScope.GLOBAL,
                used=self._committed + self._reserved,
                limit=self._limits.daily_token_ceiling,
            )

    def exhausted(self) -> bool:
        """True when another turn could not be afforded under the daily ceiling."""
        usage = self.global_usage()
        return usage.remaining < self._limits.turn_token_reservation

    def reserve(self, session_id: str) -> Reservation:
        """Claim budget for one turn, or refuse it.

        Args:
            session_id: The conversation the turn belongs to.

        Returns:
            A :class:`Reservation`. Check :attr:`Reservation.granted` before
            calling a model, and settle it afterwards.
        """
        limits = self._limits
        amount = limits.turn_token_reservation
        with self._lock:
            self._roll_over_locked()
            now = self._clock.monotonic()
            self._sweep_locked(now)
            session = self._sessions.setdefault(session_id, _SessionState())
            session.last_seen = now

            claimed = self._committed + self._reserved
            global_usage = Usage(
                scope=BudgetScope.GLOBAL,
                used=claimed,
                limit=limits.daily_token_ceiling,
            )
            if claimed + amount > limits.daily_token_ceiling:
                return self._refuse(session_id, StopReason.DAILY_CEILING, global_usage)

            if session.turns >= limits.session_turn_cap:
                return self._refuse(
                    session_id,
                    StopReason.SESSION_TURN_CAP,
                    Usage(
                        scope=BudgetScope.SESSION,
                        used=session.turns,
                        limit=limits.session_turn_cap,
                    ),
                )

            session_claimed = session.tokens + session.reserved
            if session_claimed + amount > limits.session_token_cap:
                return self._refuse(
                    session_id,
                    StopReason.SESSION_TOKEN_CAP,
                    Usage(
                        scope=BudgetScope.SESSION,
                        used=session_claimed,
                        limit=limits.session_token_cap,
                    ),
                )

            self._reserved += amount
            session.reserved += amount
            session.turns += 1
            return Reservation(
                ledger=self,
                session_id=session_id,
                tokens=amount,
                usage=Usage(
                    scope=BudgetScope.GLOBAL,
                    used=claimed + amount,
                    limit=limits.daily_token_ceiling,
                ),
                day=self._day,
            )

    def _refuse(self, session_id: str, reason: StopReason, usage: Usage) -> Reservation:
        """Build a refused reservation. Called with the lock held."""
        return Reservation(
            ledger=self,
            session_id=session_id,
            tokens=0,
            usage=usage,
            day=self._day,
            stop=Stop(reason=reason, usage=usage),
        )

    def commit(self, reservation: Reservation, tokens_used: int) -> None:
        """Commit real spend and give the reservation back.

        Called by :meth:`Reservation.settle`, which is the only sanctioned
        route: it is the half that cannot be called twice.

        Args:
            reservation: The granted reservation being settled.
            tokens_used: What the turn actually cost, already clamped.
        """
        with self._lock:
            self._roll_over_locked()
            # A turn that spanned midnight settles against today. Its
            # reservation was zeroed by the rollover, so subtracting it again
            # would credit today with spend that never happened; charging the
            # real tokens to today over-counts by less than one turn and never
            # under-counts, which is the direction to err in.
            if reservation.day == self._day:
                self._reserved = max(0, self._reserved - reservation.tokens)
                session = self._sessions.get(reservation.session_id)
                if session is not None:
                    session.reserved = max(0, session.reserved - reservation.tokens)
                    session.tokens += tokens_used
                    session.last_seen = self._clock.monotonic()
            self._committed += tokens_used

    def session_tokens(self, session_id: str) -> int:
        """Tokens settled against ``session_id`` today. Zero if it is unknown."""
        with self._lock:
            self._roll_over_locked()
            session = self._sessions.get(session_id)
            return 0 if session is None else session.tokens

    def session_turns(self, session_id: str) -> int:
        """Turns ``session_id`` has started today. Zero if it is unknown."""
        with self._lock:
            self._roll_over_locked()
            session = self._sessions.get(session_id)
            return 0 if session is None else session.turns
