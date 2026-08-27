"""The week-one ugly slice, as something the golden set can be run against.

#29's fourth acceptance criterion is *the set runs against the week-one ugly
slice, and fails honestly*, and the honesty is the deliverable. This adapter
exists to produce a bad number early, because the alternative -- waiting until
the lanes are built and then writing the set -- is trap 6, which is the trap #29
was filed to avoid.

What the slice is: ``chip_chat.agent.loop.run_turn`` over
:data:`~chip_chat.agent.hardcoded.MENU`, three items, one persona, six of the
eleven tools. So it will fail every account, personalization, cancel, redeem and
preferences case, and it will fail them with ``TOOL_NOT_IMPLEMENTED`` rather than
with a wrong answer -- which the report shows as failures with named causes
rather than as an average.

**Four signals, and the missing one is the interesting one.** This deployment
reports tools, cards, receipts and writes. It does **not** report citations,
because nothing in the request path builds a
:class:`~chip_chat.agent.envelope.ResponseEnvelope`: ``envelope.py`` exists, is
tested, and is imported by no caller, so a citation id never reaches a reply.
PRD K2's target is zero uncited claims and this deployment cannot count them.
That is a fact about the wiring rather than about the agent, and
:class:`~chip_chat.eval.golden.run.Signal` is how it stays one -- the citation
checks come back unscored, not failed. Bead ``cc-bap``.

**Each case gets its own session.** A draft minted for one case must not be
placeable from the next, and a conversation carried across cases would make the
set order-dependent -- case 12 passing because case 11 happened to leave the
right thing on screen is not a result. One conversation, one desk, one session
id per case.

**A confirmed case gets a real confirmation.** Where a case sets ``confirmed``,
this mints a draft on the desk, confirms it the way the request handler does --
:meth:`~chip_chat.agent.orders.OrderDesk.confirm`, which no tool can reach --
and substitutes the real draft id into the case's context and message wherever
they write ``{draft_id}``. A fabricated id in a prose string would test nothing:
``place_order`` would refuse it, correctly, and the case would fail for the
wrong reason.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from chip_chat.agent.hardcoded import ACCOUNT, MENU
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.loop import Conversation, TurnResult, run_turn
from chip_chat.agent.model import ChatModel
from chip_chat.agent.orders import Draft, OrderDesk
from chip_chat.agent.tools import offered_tools
from chip_chat.eval.golden.cases import ANY_PERSONA, GoldenCase
from chip_chat.eval.golden.run import DEFAULT_SESSION, Observation, Signal
from chip_chat.otel import chat_turn

__all__ = ["SLICE_PERSONA", "SLICE_SIGNALS", "SliceDeployment"]

SLICE_PERSONA: Final = "regular"
"""Which of ``population.toml``'s archetypes the slice's one account is.

``hardcoded.ACCOUNT`` calls itself ``persona-loyal-regular`` -- eighteen months
at one store, an unambiguous usual, 1,340 points. That is the Regular, and a
case written for the Lapsed Customer or the Explorer has no account here to be
right about. Such a case is refused rather than run: scoring *"what's my usual"*
for a low-confidence persona against a high-confidence one measures the fixture.
"""

SLICE_SIGNALS: Final[frozenset[Signal]] = frozenset(
    {Signal.TOOLS, Signal.CARD, Signal.RECEIPT, Signal.WRITES}
)
"""What this deployment can observe. See the module docstring on the fifth."""

_DRAFT_PLACEHOLDER: Final = "{draft_id}"


@dataclass(frozen=True, slots=True)
class SliceDeployment:
    """The in-process agent loop, wearing the runner's seam.

    Attributes:
        model: The chat model to run the loop against. A real deployment
            produces a real number; a scripted double produces a measurement of
            the script, which is what ``chip_chat.eval.photos.testing`` says at
            greater length and is no less true here.
        lanes: The backing services the slice runs against. Without the photo
            lane the slice is not offered ``match_meal_from_photo`` at all, and
            the vision routing case fails for the honest reason that the tool is
            absent; the same is true of the account and personalization cases
            and their two tools.
        session_prefix: What each case's session id is built from.
    """

    model: ChatModel
    lanes: Lanes = NO_LANES
    session_prefix: str = DEFAULT_SESSION

    @property
    def name(self) -> str:
        """The deployment, as the report names it."""
        return f"week-one slice on {self.model.deployment}"

    @property
    def reports(self) -> frozenset[Signal]:
        """The four signals of :data:`SLICE_SIGNALS`."""
        return SLICE_SIGNALS

    def turn(self, case: GoldenCase) -> Observation:
        """Run one case through the loop and report what happened.

        Args:
            case: The case.

        Returns:
            The observation, or one carrying an ``error`` where the case
            presumes a persona this slice does not serve.
        """
        if case.persona not in (ANY_PERSONA, SLICE_PERSONA):
            return Observation(
                case_id=case.case_id,
                error=(
                    f"this slice serves the {SLICE_PERSONA} persona "
                    f"({ACCOUNT.persona_id}); the case presumes {case.persona}"
                ),
                reports=self.reports,
            )

        session_id = f"{self.session_prefix}-{case.case_id}"
        desk = OrderDesk()
        conversation = Conversation(
            session_id=session_id, tools=offered_tools(self.lanes)
        )
        draft = self._confirmed_draft(case, session_id, desk)
        draft_id = None if draft is None else draft.draft_id
        conversation.messages.extend(
            {"role": "assistant", "content": _fill(line, draft_id)}
            for line in case.context
        )

        before = len(conversation.messages)
        with chat_turn(session_id=session_id, turn_index=0, message=case.message):
            result = run_turn(
                conversation,
                _fill(case.message, draft_id),
                model=self.model,
                desk=desk,
                lanes=self.lanes,
                confirmed_draft_id=draft_id,
            )
        return self._observed(case, result, conversation.messages[before:])

    def _confirmed_draft(
        self, case: GoldenCase, session_id: str, desk: OrderDesk
    ) -> Draft | None:
        """Put a confirmed draft on the desk, where the case says there is one.

        The items are the slice's own menu rather than anything the case names,
        because what a confirmed case is testing is the *ordering* -- that a
        write follows a confirmation -- and not which bowl it was.
        """
        if not case.confirmed:
            return None
        item_id = next(iter(MENU))
        draft = desk.propose(session_id, [{"item_id": item_id, "quantity": 1}])
        return desk.confirm(session_id, draft.draft_id)

    def _observed(
        self,
        case: GoldenCase,
        result: TurnResult,
        emitted: Sequence[Mapping[str, Any]],
    ) -> Observation:
        """Read the turn back as an observation.

        The tools come off the messages the loop appended rather than off a
        span exporter, because the messages are what the loop actually sent and
        an adapter that read them from somewhere else could disagree with the
        conversation the model saw.
        """
        return Observation(
            case_id=case.case_id,
            reply=result.reply,
            tools=_tools_called(emitted),
            card=result.card,
            receipt=result.receipt,
            # The slice implements exactly one write, `place_order`, and a
            # receipt is what it returns when the ops path accepted the draft.
            # A refused write -- an unconfirmed draft, an expired one, one from
            # another session -- returns a rejection the model reads out, and
            # no receipt. So a receipt is proof a write executed, and its
            # absence is proof none did.
            wrote=result.receipt,
            reports=self.reports,
        )


def _fill(text: str, draft_id: str | None) -> str:
    """Substitute the real draft id into a case's prose, where it asks for one."""
    if draft_id is None or _DRAFT_PLACEHOLDER not in text:
        return text
    return text.replace(_DRAFT_PLACEHOLDER, draft_id)


def _tools_called(messages: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every tool the loop called on this turn, in call order."""
    called: list[str] = []
    for message in messages:
        for call in message.get("tool_calls") or ():
            name = call.get("function", {}).get("name")
            if isinstance(name, str):
                called.append(name)
    return tuple(called)
