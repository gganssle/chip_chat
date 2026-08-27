"""The only route from a request to a model, and why there is only one.

``cc-fv1`` shipped :class:`~chip_chat.api.guard.SpendGuard` correct, tested, and
with no caller, because the request path did not exist yet. The best it could
offer was a guard somebody would remember to call. This module is what that
turns into once the call site exists, and it takes the shape ``cc-h80`` used for
image moderation: the wrong ordering is not merely discouraged, it is
unstateable.

Three facts, and between them there is no route to a model that skips the check:

**A** :class:`FundedTurn` **cannot exist for a turn the guard refused.** Its
constructor raises on a budget that is not allowed, so holding one *is* the
proof that the four spend layers ran and said yes.

**A** :class:`SpendGate` **cannot be built without a guard.** It is a required
positional argument, so there is no configuration of this package in which the
check is absent.

**Nothing else here exposes a model.** :class:`SpendGate` builds one lazily and
keeps it private; the only public method that reaches it is
:meth:`FundedTurn.run`. A second route added to the app later cannot call a
model without going through :meth:`SpendGate.turn` first, because there is
nothing else to call.

The fourth property is that :meth:`FundedTurn.run` settles the turn's real token
cost itself. A caller cannot forget to, so the ceiling counts tokens rather than
turns.

.. code-block:: python

    with gate.turn(session_id=sid, source_address=ip) as funded:
        if isinstance(funded, Stop):
            return stop_state(funded.message)
        result = funded.run(conversation, message)

``api/tests/test_spend_gate.py`` is the half of this that a future contributor
actually feels: those tests fail when the *invariant* breaks, not when the
output changes.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.loop import Conversation, TurnResult, run_turn
from chip_chat.agent.model import ChatModel
from chip_chat.agent.orders import OrderDesk
from chip_chat.api.guard import SpendGuard, TurnBudget
from chip_chat.api.outcome import Stop

__all__ = ["FundedTurn", "SpendGate", "UnfundedTurnError"]


class UnfundedTurnError(RuntimeError):
    """A :class:`FundedTurn` was built for a turn the guard refused.

    Always a programming error. It means somebody constructed the object that
    is supposed to *be* the proof of an allowed budget without one, which is
    the single mistake this module exists to make impossible.
    """


class FundedTurn:
    """One turn whose budget was checked and allowed, and the model it may call.

    The constructor is the enforcement. Everything else is bookkeeping.
    """

    __slots__ = ("_budget", "_desk", "_lanes", "_model")

    def __init__(
        self,
        budget: TurnBudget,
        model: ChatModel,
        desk: OrderDesk,
        lanes: Lanes = NO_LANES,
    ) -> None:
        """Bind an allowed budget to the model it paid for.

        Args:
            budget: The turn's budget, which must be allowed.
            model: The chat model this turn may call.
            desk: The order desk holding this session's drafts.
            lanes: The backing services this deployment has.

        Raises:
            UnfundedTurnError: If ``budget`` is not allowed. There is no such
                thing as a funded turn the guard refused.
        """
        if not budget.allowed:
            raise UnfundedTurnError(
                "a FundedTurn is proof of an allowed budget; this one was "
                f"refused with {budget.stop.reason.value if budget.stop else '?'}"
            )
        self._budget = budget
        self._model = model
        self._desk = desk
        self._lanes = lanes

    @property
    def tokens_used(self) -> int:
        """Tokens charged to this turn so far."""
        return self._budget.tokens_used

    def run(
        self,
        conversation: Conversation,
        message: str,
        *,
        confirm_draft_id: str | None = None,
    ) -> TurnResult:
        """Run the agent, and charge the ceiling what it actually cost.

        The settlement is not the caller's to remember: a turn whose tokens went
        unrecorded would leave the daily ceiling counting turns rather than
        tokens, and it would be wrong in the expensive direction.

        Args:
            conversation: The visitor's history, appended to in place.
            message: What the visitor said.
            confirm_draft_id: A draft the visitor confirmed by pressing the
                button. Applied before the agent runs, so ``place_order`` finds
                a confirmed draft rather than being told about one. Nothing the
                model says can reach this argument.

        Returns:
            The turn's result.
        """
        if confirm_draft_id:
            self._desk.confirm(conversation.session_id, confirm_draft_id)
        result = run_turn(
            conversation,
            message,
            model=self._model,
            desk=self._desk,
            lanes=self._lanes,
        )
        self._budget.record_usage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        return result

    def charge_reservation(self, reservation: int) -> None:
        """Charge the pessimistic estimate for a turn that failed mid-flight.

        What a broken turn bought before it fell over is unknown. Over-counting
        by less than one turn is the safe direction to be wrong in; under-
        counting is how a ceiling quietly stops being one.
        """
        self._budget.record_usage(prompt_tokens=reservation)


class SpendGate:
    """The guard, the model, and the only door between them.

    One per process. Building the model is deferred to first use because it
    authenticates against Azure, and a process that cannot reach Azure should
    still start, serve ``/healthz`` and say what is wrong.
    """

    __slots__ = ("_desk", "_guard", "_lanes", "_model", "_model_factory")

    def __init__(
        self,
        guard: SpendGuard,
        model_factory: Callable[[], ChatModel],
        *,
        desk: OrderDesk | None = None,
        lanes: Lanes = NO_LANES,
    ) -> None:
        """Assemble the gate.

        Args:
            guard: The spend cap. Positional and required: there is no gate
                without one, which is the point of the class.
            model_factory: Builds the chat model on first use.
            desk: The order desk. Defaults to a fresh one.
            lanes: The backing services this deployment has. The default is
                nothing wired, which withdraws ``ask_account_question``,
                ``get_recommendations`` and ``match_meal_from_photo`` -- the
                honest state for a deployment with none of them behind it.
        """
        self._guard = guard
        self._model_factory = model_factory
        self._desk = desk if desk is not None else OrderDesk()
        self._lanes = lanes
        self._model: ChatModel | None = None

    @property
    def guard(self) -> SpendGuard:
        """The cap this gate enforces, for an ops surface that reports on it."""
        return self._guard

    @property
    def desk(self) -> OrderDesk:
        """The order desk. Holds drafts; cannot call a model."""
        return self._desk

    @property
    def lanes(self) -> Lanes:
        """What is wired, which is what the agent may be offered."""
        return self._lanes

    def entry_state(self) -> Stop | None:
        """Whether a visitor may start a conversation at all. Emits no span."""
        return self._guard.entry_state()

    @contextmanager
    def turn(
        self, *, session_id: str, source_address: str
    ) -> Iterator["FundedTurn | Stop"]:
        """Check the budget for one turn and, if it holds, open the door.

        Must be called inside a ``chat.turn``: ``guard.budget_check`` is a child
        of it.

        Args:
            session_id: The conversation this turn belongs to.
            source_address: The client address the rate limit counts against.

        Yields:
            A :class:`FundedTurn` when the turn may proceed, or the
            :class:`~chip_chat.api.outcome.Stop` that refused it. A ``Stop`` has
            no ``run``, so the refused branch cannot accidentally call a model.
        """
        with self._guard.turn(
            session_id=session_id, source_address=source_address
        ) as budget:
            if not budget.allowed:
                # `budget.allowed` is false, so `budget.stop` is set; the
                # fallback exists only to keep the type honest.
                assert budget.stop is not None
                yield budget.stop
                return
            yield FundedTurn(budget, self._model_for_this_turn(), self._desk, self._lanes)

    def _model_for_this_turn(self) -> ChatModel:
        """Build the model on first use and keep it. Private, and stays private.

        A public accessor here would be a second route to a model, and the
        whole claim of this module is that there is only one.
        """
        if self._model is None:
            self._model = self._model_factory()
        return self._model
