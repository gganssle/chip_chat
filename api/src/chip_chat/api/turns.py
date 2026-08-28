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

from chip_chat.agent.desk import Desk
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.loop import Conversation, TurnResult, run_turn
from chip_chat.agent.model import ChatModel
from chip_chat.agent.orders import OrderDesk
from chip_chat.api.guard import SpendGuard, TurnBudget
from chip_chat.api.moderation import (
    BLOCKED_MESSAGE,
    ModerationUnavailableError,
    TextModerator,
)
from chip_chat.api.outcome import Stop, StopReason

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
        desk: Desk,
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
            confirm_draft_id: The card the visitor confirmed by pressing the
                button. Applied before the agent runs, so ``place_order`` finds
                a confirmed record rather than being told about one. Nothing the
                model says can reach this argument.

                One field for both record types, and the name is the older of
                the two rather than a lie. A browser presses one button on one
                card and has no way of knowing -- and no business knowing --
                whether the id on it is a draft's or a confirmation's;
                :meth:`chip_chat.agent.desk.Desk.confirm` is what resolves that,
                and it looks in the store this visitor's records are actually
                in. Renaming the field was considered and rejected: it is the
                one the deployed widget posts and the one the live write-gate
                red team composes, and a rename would have quietly unscored
                every probe in that suite.

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

    __slots__ = ("_desk", "_guard", "_lanes", "_model", "_model_factory", "_moderator")

    def __init__(
        self,
        guard: SpendGuard,
        model_factory: Callable[[], ChatModel],
        *,
        desk: Desk | None = None,
        lanes: Lanes = NO_LANES,
        moderator: TextModerator | None = None,
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
            moderator: Screens inbound text (#79). Defaults to one over
                :class:`~chip_chat.api.moderation.LocalTextAnalyzer`, so a
                deployment with no Content Safety endpoint still screens rather
                than silently not screening. **There is no accessor for it**,
                for the same reason there is none for the model: a second route
                to the moderator is a second route around it.
        """
        self._guard = guard
        self._model_factory = model_factory
        self._desk = desk if desk is not None else OrderDesk()
        self._lanes = lanes
        self._model: ChatModel | None = None
        self._moderator = moderator if moderator is not None else TextModerator()

    @property
    def guard(self) -> SpendGuard:
        """The cap this gate enforces, for an ops surface that reports on it."""
        return self._guard

    @property
    def desk(self) -> Desk:
        """The action lane. Holds drafts; cannot call a model.

        Typed as the protocol rather than as the week-one class, which is what
        lets :func:`chip_chat.api.app.build_service` hand in
        :class:`~chip_chat.api.orderdesk.OpsDesk` -- the same store, the real
        catalogue, and the deployed ops API behind it -- without this module
        knowing anything about either.
        """
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
        self, *, session_id: str, source_address: str, message: str = ""
    ) -> Iterator["FundedTurn | Stop"]:
        """Check the budget and the content, and only then open the door.

        Must be called inside a ``chat.turn``: ``guard.budget_check`` and
        ``guard.content_safety`` are both children of it.

        **The order here is the enforcement.** The budget is checked, then the
        text is moderated, and only after both does a :class:`FundedTurn` come
        into existence. A ``Stop`` has no ``run``, so neither refused branch can
        call a model -- not because a later reader remembered to return early,
        but because there is nothing on the object to call. That is what makes
        #79's *nothing unmoderated reaches a model* a property of the type
        rather than of this function's control flow.

        Moderation runs second, after the budget. A turn refused for spend
        should not pay for a moderation call, and the ceiling is the cheaper
        check.

        Args:
            session_id: The conversation this turn belongs to.
            source_address: The client address the rate limit counts against.
            message: The visitor's inbound text, screened before the model sees
                it. Empty screens nothing, which is right for the callers that
                open a turn around something other than a typed message -- the
                photo route opens one around an upload it has already moderated
                as an image.

        Yields:
            A :class:`FundedTurn` when the turn may proceed, or the
            :class:`~chip_chat.api.outcome.Stop` that refused it.
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
            refusal = self._screen(message, budget)
            if refusal is not None:
                yield refusal
                return
            yield FundedTurn(budget, self._model_for_this_turn(), self._desk, self._lanes)

    def _screen(self, message: str, budget: TurnBudget) -> Stop | None:
        """Moderate ``message``, or return the ``Stop`` that refuses the turn.

        Two outcomes that look alike and are not. A message Content Safety
        *flags* is declined with :data:`~chip_chat.api.moderation.BLOCKED_MESSAGE`
        and the conversation continues. A moderator that could not be *reached*
        also refuses -- fails closed -- because the alternative on an
        unauthenticated public endpoint is that a Content Safety outage silently
        becomes no moderation at all.

        The outage path is caught here rather than left to the request handler:
        ``app.py`` wraps the turn in a broad ``except Exception`` that would
        swallow a ``ModerationUnavailableError`` and serve an apology, which
        looks exactly like failing closed and is not -- the check would simply
        be skipped again on the retry.
        """
        if not message:
            return None
        try:
            verdict = self._moderator.screen(message, subject="user_prompt")
        except ModerationUnavailableError:
            return Stop(
                reason=StopReason.MODERATION_UNAVAILABLE,
                usage=budget.usage,
                message=BLOCKED_MESSAGE,
            )
        if verdict.blocked:
            return Stop(
                reason=StopReason.CONTENT_BLOCKED,
                usage=budget.usage,
                message=BLOCKED_MESSAGE,
            )
        return None

    def _model_for_this_turn(self) -> ChatModel:
        """Build the model on first use and keep it. Private, and stays private.

        A public accessor here would be a second route to a model, and the
        whole claim of this module is that there is only one.
        """
        if self._model is None:
            self._model = self._model_factory()
        return self._model
