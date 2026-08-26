"""The inline spend cap: one synchronous check in front of every model call.

This module is the answer to RFC-001 section 11, and the distinction it turns on
is worth restating because it is easy to blur. Azure budget alerts notify after
the fact. Arize reports what was spent. Neither prevents anything. The check
below runs *in the request path, before the agent is invoked*, and its refusal
is the reason no tokens are bought. If an implementation of this could fairly be
described as observability, it is the wrong implementation.

Four layers, evaluated in this order:

1. The circuit breaker (:mod:`chip_chat.api.killswitch`) -- a manual flip beats
   every other consideration and costs nothing to check.
2. The per-source-address rate limit -- the abuse layer, ahead of the ceilings
   it exists to protect.
3. The global daily token ceiling.
4. The per-session turn and token caps.

Every one of them refuses the same way: :data:`~chip_chat.api.outcome.STOP_STATE_MESSAGE`,
a designed state and never an error, on entry and mid-conversation alike.

Typical use, inside whatever the request handler already opened::

    with chat_turn(session_id=sid, turn_index=n, message=text) as turn:
        with guard.turn(session_id=sid, source_address=ip) as budget:
            if not budget.allowed:
                turn.record_output(budget.message)
                return stop_state_response(budget.message)
            reply = agent.run(text)                  # only reached when allowed
            budget.record_usage(prompt_tokens=p, completion_tokens=c)

``guard.budget_check`` is a child of ``chat.turn``, so :meth:`SpendGuard.turn`
must be called inside one; :meth:`SpendGuard.entry_state` answers the same
question without a span, for the entry page where no turn exists yet.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from chip_chat.api.clock import Clock, SystemClock
from chip_chat.api.killswitch import KillSwitch, ManualKillSwitch
from chip_chat.api.ledger import BudgetLedger, Reservation
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import Stop, StopReason, Usage
from chip_chat.api.ratelimit import SourceRateLimiter
from chip_chat.otel import budget_check
from chip_chat.otel.spans import GuardRecorder

__all__ = ["SpendGuard", "TurnBudget"]


class TurnBudget:
    """What one turn holds between the check and the accounting.

    :attr:`allowed` is the whole interface as far as correctness goes: when it
    is false the request path must not call a model. The rest is bookkeeping --
    the tokens the turn really cost, reported back so the ceiling reflects
    reality rather than the pessimistic reservation the check made.
    """

    __slots__ = ("_reservation", "_tokens_used")

    def __init__(self, reservation: Reservation) -> None:
        self._reservation = reservation
        self._tokens_used = 0

    @property
    def allowed(self) -> bool:
        """True when the turn may proceed and a model may be called."""
        return self._reservation.granted

    @property
    def stop(self) -> Stop | None:
        """Why the turn was refused, or ``None`` when it was allowed."""
        return self._reservation.stop

    @property
    def usage(self) -> Usage:
        """The ceiling that decided this turn, and how close it is."""
        return self._reservation.usage

    @property
    def message(self) -> str | None:
        """The copy to show the visitor, or ``None`` when the turn may proceed."""
        stop = self._reservation.stop
        return None if stop is None else stop.message

    @property
    def tokens_used(self) -> int:
        """Tokens reported against this turn so far."""
        return self._tokens_used

    def record_usage(self, *, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """Add one model round trip's tokens to this turn's total.

        Call it once per ``llm.completion``; the totals accumulate, so a turn
        that took four steps is charged for four steps.

        Args:
            prompt_tokens: Input tokens the model billed for.
            completion_tokens: Output tokens the model billed for.
        """
        self._tokens_used += max(0, prompt_tokens) + max(0, completion_tokens)

    def settle(self) -> None:
        """Charge the ceiling with what the turn actually cost.

        :meth:`SpendGuard.turn` does this for you. It is public for the caller
        that cannot hold a ``with`` block across its own await points, and it is
        idempotent so the two cannot double-count.
        """
        self._reservation.settle(self._tokens_used)


class SpendGuard:
    """The four spend-control layers, evaluated as one synchronous decision."""

    __slots__ = ("_kill_switch", "_ledger", "_limits", "_rate_limiter")

    def __init__(
        self,
        limits: SpendLimits | None = None,
        *,
        ledger: BudgetLedger | None = None,
        rate_limiter: SourceRateLimiter | None = None,
        kill_switch: KillSwitch | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Assemble a guard.

        Args:
            limits: The ceilings to enforce. Defaults to :class:`SpendLimits`.
            ledger: Token counters. Defaults to a fresh :class:`BudgetLedger`.
            rate_limiter: Per-source limiter. Defaults to a fresh one.
            kill_switch: The circuit breaker. Defaults to an in-process
                :class:`~chip_chat.api.killswitch.ManualKillSwitch`, which a
                deployment should replace with one whose source outlives the
                process.
            clock: Source of time for the components built here. Ignored for
                any component passed in, which brings its own.
        """
        # A ledger brings its own ceilings; adopting them keeps `guard.limits`
        # from reporting numbers the guard is not actually enforcing.
        if limits is not None:
            self._limits = limits
        elif ledger is not None:
            self._limits = ledger.limits
        else:
            self._limits = SpendLimits()
        the_clock: Clock = clock if clock is not None else SystemClock()
        self._ledger = (
            ledger if ledger is not None else BudgetLedger(self._limits, the_clock)
        )
        self._rate_limiter = (
            rate_limiter
            if rate_limiter is not None
            else SourceRateLimiter(self._limits, the_clock)
        )
        self._kill_switch = kill_switch if kill_switch is not None else ManualKillSwitch()

    @property
    def limits(self) -> SpendLimits:
        """The ceilings this guard enforces."""
        return self._limits

    @property
    def ledger(self) -> BudgetLedger:
        """The token counters, for an ops surface that wants to report them."""
        return self._ledger

    @property
    def rate_limiter(self) -> SourceRateLimiter:
        """The per-source limiter."""
        return self._rate_limiter

    @property
    def kill_switch(self) -> KillSwitch:
        """The circuit breaker."""
        return self._kill_switch

    def entry_state(self) -> Stop | None:
        """Decide whether a visitor may start a conversation at all.

        Emits no span, because there is no turn to hang one under yet. Checks
        only what is knowable before a session exists: the circuit breaker and
        the global daily ceiling.

        Returns:
            A :class:`~chip_chat.api.outcome.Stop` to render on entry, or
            ``None`` when the door is open.
        """
        usage = self._ledger.global_usage()
        if self._kill_switch.is_thrown():
            return Stop(reason=StopReason.KILL_SWITCH, usage=usage)
        if self._ledger.exhausted():
            return Stop(reason=StopReason.DAILY_CEILING, usage=usage)
        return None

    def reserve(self, *, session_id: str, source_address: str) -> TurnBudget:
        """Run the check for one turn and claim its budget.

        Opens and closes ``guard.budget_check``, so it must be called inside a
        ``chat.turn``. The returned budget must be settled -- prefer
        :meth:`turn`, which cannot forget.

        Args:
            session_id: The conversation this turn belongs to.
            source_address: The client address, already resolved from whatever
                proxy headers the deployment trusts.

        Returns:
            The turn's :class:`TurnBudget`.
        """
        with budget_check() as recorder:
            reservation = self._decide(session_id, source_address)
            _record(recorder, reservation)
            return TurnBudget(reservation)

    @contextmanager
    def turn(self, *, session_id: str, source_address: str) -> Iterator[TurnBudget]:
        """Hold the budget for one turn, settling it however the turn ends.

        The reservation is returned even when the body raises, so a turn that
        died halfway does not hold tokens against the ceiling until midnight.

        Args:
            session_id: The conversation this turn belongs to.
            source_address: The client address.

        Yields:
            The turn's :class:`TurnBudget`. Check :attr:`TurnBudget.allowed`
            before calling a model.
        """
        budget = self.reserve(session_id=session_id, source_address=source_address)
        try:
            yield budget
        finally:
            budget.settle()

    def _decide(self, session_id: str, source_address: str) -> Reservation:
        """Evaluate the four layers in order and return the resulting claim."""
        if self._kill_switch.is_thrown():
            return _refused(
                session_id,
                Stop(
                    reason=StopReason.KILL_SWITCH,
                    usage=self._ledger.global_usage(),
                ),
            )
        rate_limited = self._rate_limiter.check(source_address)
        if rate_limited is not None:
            return _refused(session_id, rate_limited)
        return self._ledger.reserve(session_id)


def _refused(session_id: str, stop: Stop) -> Reservation:
    """Wrap a stop from outside the ledger in the handle a reserve returns.

    Callers should not have to care which layer said no, and a reservation that
    holds no ledger settles to nothing, so the unwinding path is identical.
    """
    return Reservation(
        ledger=None,
        session_id=session_id,
        tokens=0,
        usage=stop.usage,
        stop=stop,
    )


def _record(recorder: GuardRecorder, reservation: Reservation) -> None:
    """Stamp the decision onto ``guard.budget_check``."""
    usage = reservation.usage
    recorder.record_budget(
        scope=usage.scope.value,
        tokens_used=usage.used,
        tokens_limit=usage.limit,
    )
    stop = reservation.stop
    if stop is None:
        recorder.allow()
    else:
        recorder.block(stop.reason.value)
