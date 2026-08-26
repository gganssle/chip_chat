"""What the visitor is told, for the three photographs that are not the happy path.

The happy path is the easy part. Issue #55 is the other three, and each has a
specified behaviour rather than a default:

**Food that is not this restaurant's food.** PRD V4 asks for two things in one
breath -- say so, *and* offer the closest thing that is available. Not a refusal,
and not a silent best-effort match either. The offer is composed in
:mod:`chip_chat.vision.matcher` from catalogue rows; this module puts it in a
sentence.

**A component the model was not sure of.** PRD V5 asks a clarifying question
rather than a guess, and PRD V3 says what the question has to contain: what it
believes it saw, in the visitor's language, so they can correct it before
confirming. The hedge names the *specific* uncertain slot. A generic "I might
be wrong" is a different behaviour with the same tone, and it is not this one.

**Several meals in one frame.** ``docs/decisions/multi-meal-photos.md`` decided
it: say how many, say plainly that this builds one order at a time, and ask
which. Never a draft -- a draft from a table of four bowls is composed entirely
of real catalogue items and is an order nobody in the picture is eating, which
is the worst failure shape this product has.

Why this module holds no catalogue
----------------------------------

Every food word in every sentence below arrives on the
:class:`~chip_chat.vision.matcher.Resolution` -- as
:attr:`~chip_chat.vision.matcher.ResolvedItem.name`, which is a published name,
or as :attr:`~chip_chat.vision.matcher.SeenSlot.name`, which is the published
name a vocabulary term was derived from. :func:`reply_for` takes a resolution
and nothing else. It has no catalogue to look anything up in, no vocabulary, and
no client.

That is PRD V6 -- *never names a menu item that does not exist* -- arranged
rather than promised, and it is the same move stage 4 makes with ``notes``. A
rule saying "only interpolate catalogue names here" is obeyed until somebody is
in a hurry and wants a nicer word for one item. A function with no catalogue in
scope cannot look one up to be nicer about.

The slot words are the exception that proves it: ``the protein``, ``the rice``,
``what it's served in``. Those name RFC-001 section 07's *slots*, which are a
fixed enum in :class:`~chip_chat.catalog.records.Slot` and not a menu --
:func:`slot_noun` covers every member of it, and ``tests/test_reply.py`` fails
if a new slot ever arrives without one.

What is not here
----------------

The confirmation card is issue #62. :attr:`Reply.text` is the sentence above it,
which on a resolved photograph is PRD V3's "states what it believes it saw"
and nothing more -- no price, no draft id, no promise about what a button does.

The model's own ``notes`` is not here either, and is not read here. It is
display-only prose that belongs to whatever renders the turn, and it reaches
that renderer from :attr:`~chip_chat.vision.describe.Description.notes`
directly, the way ``vision/README.md`` shows. A reply that quoted it would make
this module the second reader of the one field nothing downstream may parse.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, assert_never

from chip_chat.catalog.records import Slot
from chip_chat.vision.matcher import (
    Clarification,
    ClarificationReason,
    Outcome,
    Resolution,
    ResolvedItem,
    SeenSlot,
)

__all__ = [
    "SLOT_NOUNS",
    "Reply",
    "ReplyKind",
    "reply_for",
    "slot_noun",
]


SLOT_NOUNS: Final[Mapping[Slot, str]] = {
    Slot.VESSEL: "what it's served in",
    Slot.PROTEIN: "the protein",
    Slot.RICE: "the rice",
    Slot.BEANS: "the beans",
    Slot.SALSAS: "the salsa",
    Slot.TOPPINGS: "the toppings",
}
"""How each slot is referred to in a question, in the visitor's words.

Not a menu. These name the six slots of RFC-001 section 07's schema, which are
:class:`~chip_chat.catalog.records.Slot` -- a fixed enum that changes when the
schema changes and not when the menu does. Nothing in here is a food.

``vessel`` is the one that needed thinking about. The natural sentence is "was
that a bowl or a burrito", and those are catalogue terms: writing them here
would put a hand-maintained menu in the file the menu build does not touch, and
it would be wrong the first time a vessel is added. So the question asks about
*what it's served in* and lets the visitor supply the word, which is what the
next turn resolves anyway.
"""


class ReplyKind(StrEnum):
    """What kind of turn this is, for a caller that renders them differently.

    A caller could switch on :attr:`Resolution.outcome` instead, and the reason
    not to is that these are not the same question. An outcome is what stage 5
    concluded; a kind is what the visitor is being asked to do about it -- and
    :attr:`PROPOSAL` and :attr:`ALTERNATIVE` both put items on the screen while
    being opposite conclusions about the photograph.

    Attributes:
        PROPOSAL: The photograph resolved. :attr:`Reply.items` is the draft, and
            the card under it is issue #62's.
        QUESTION: A required slot could not be believed. :attr:`Reply.asks_about`
            names which, and the text asks about it by name.
        ALTERNATIVE: The food is not this restaurant's. :attr:`Reply.items` is
            the closest available meal, offered rather than proposed.
        ONE_AT_A_TIME: Several meals in the frame. Nothing was built and nothing
            is offered.
    """

    PROPOSAL = "proposal"
    QUESTION = "question"
    ALTERNATIVE = "alternative"
    ONE_AT_A_TIME = "one_at_a_time"


@dataclass(frozen=True, slots=True)
class Reply:
    """One turn's sentence, and the structured facts behind it.

    Attributes:
        kind: See :class:`ReplyKind`.
        text: What to say. Every food word in it is a published catalogue name.
        items: The rows the text names, in the order it names them: the draft on
            :attr:`ReplyKind.PROPOSAL`, the offer on
            :attr:`ReplyKind.ALTERNATIVE`, and empty otherwise. A caller that
            renders a card reads this rather than parsing :attr:`text`.
        asks_about: The slots :attr:`text` asks a question about, in the order
            it asks. Empty on every kind but :attr:`ReplyKind.QUESTION`.
        meals_visible: What the model counted, carried so that a caller can
            record it. Only meaningful on :attr:`ReplyKind.ONE_AT_A_TIME`.
    """

    kind: ReplyKind
    text: str
    items: tuple[ResolvedItem, ...] = ()
    asks_about: tuple[Slot, ...] = ()
    meals_visible: int = 0

    @property
    def builds_nothing(self) -> bool:
        """Whether this turn puts no items in front of the visitor at all."""
        return not self.items


def slot_noun(slot: Slot) -> str:
    """Return how a question refers to one slot.

    Args:
        slot: The slot.

    Returns:
        The noun phrase, e.g. ``the protein``.

    Raises:
        KeyError: If the slot has no phrasing. A slot added to the schema
            without one must not silently become its own enum value in a
            sentence a stranger reads.
    """
    try:
        return SLOT_NOUNS[slot]
    except KeyError:
        raise KeyError(f"no visitor-facing phrasing for the {slot.value} slot") from None


def reply_for(resolution: Resolution) -> Reply:
    """Turn one stage-5 conclusion into what the visitor is told.

    Total over :class:`~chip_chat.vision.matcher.Outcome` on purpose: a new
    outcome added to stage 5 without a sentence here is a type error under
    ``mypy`` rather than a photograph that gets no answer.

    Args:
        resolution: What stage 5 concluded. The only argument, and the reason
            this function cannot name an item the catalogue does not publish --
            see the module docstring.

    Returns:
        The :class:`Reply`.
    """
    match resolution.outcome:
        case Outcome.RESOLVED:
            return _proposal(resolution)
        case Outcome.CLARIFY:
            return _question(resolution)
        case Outcome.NOT_ORDERABLE:
            return _alternative(resolution)
        case Outcome.SEVERAL_MEALS:
            return _one_at_a_time(resolution)
        case unreached:  # pragma: no cover - the exhaustiveness check itself
            assert_never(unreached)


# --- the four turns ---------------------------------------------------------


def _proposal(resolution: Resolution) -> Reply:
    """Say what was believed, and invite a correction before anything happens.

    PRD V3's whole sentence: *"states what it believes it saw, in the visitor's
    language, so they can correct it before confirming."* The correcting and the
    confirming are the card's, which is issue #62; the stating is this.
    """
    items = resolution.items()
    return Reply(
        kind=ReplyKind.PROPOSAL,
        text=(
            f"It looks like {_composition(items)} — tell me if I've got any of "
            f"that wrong before I put it in."
        ),
        items=items,
        meals_visible=resolution.meals_visible,
    )


def _question(resolution: Resolution) -> Reply:
    """Say what was made out, then ask about exactly what was not.

    Two halves, and both are requirements. The first is PRD V3 and it is what
    stops the question reading as though nothing was looked at. The second is
    PRD V5 and issue #55's third acceptance criterion: the hedge names the
    uncertain slot, so that a visitor who reads only the last sentence still
    knows which answer is being asked for.

    Nothing is proposed. A question with a draft under it is a guess with a
    disclaimer, which is the behaviour V5 exists to rule out.
    """
    asked = tuple(clarification.slot for clarification in resolution.clarifications)
    sentences = [
        *_believed(resolution.seen, without=set(asked)),
        *_asks(resolution.clarifications),
    ]
    return Reply(
        kind=ReplyKind.QUESTION,
        text=" ".join(sentences),
        asks_about=asked,
        meals_visible=resolution.meals_visible,
    )


def _alternative(resolution: Resolution) -> Reply:
    """Say it is not something this restaurant makes, and offer what it does.

    PRD V4 is two clauses and both are load-bearing. Dropping the first is a
    silent best-effort match -- the visitor gets a burrito bowl and no idea that
    what they photographed was never on the menu. Dropping the second is a bare
    refusal, which is what issue #55 rules out in the same sentence that asks
    for the first.

    Both branches below end in a next step, because a restaurant with nothing
    available is still not a reason to end a turn with "no".
    """
    offer = resolution.alternative
    if not offer:
        return Reply(
            kind=ReplyKind.ALTERNATIVE,
            text=(
                "That isn't something we make, and I can't see anything on the "
                "menu here to offer in its place right now. Tell me what you're "
                "after and I'll check."
            ),
            meals_visible=resolution.meals_visible,
        )
    return Reply(
        kind=ReplyKind.ALTERNATIVE,
        text=(
            f"That isn't something we make. The closest we do is "
            f"{_composition(offer)} — say the word and I'll start there, or "
            f"tell me what you're after instead."
        ),
        items=offer,
        meals_visible=resolution.meals_visible,
    )


def _one_at_a_time(resolution: Resolution) -> Reply:
    """Say how many were seen, and ask which one, in both modalities.

    ``docs/decisions/multi-meal-photos.md`` states three properties of this
    turn as requirements rather than as copy: it names the count it saw, so the
    visitor can correct an observation rather than argue with a category; it
    offers a concrete next step in both of the ways one is available; and it
    never silently builds.
    """
    return Reply(
        kind=ReplyKind.ONE_AT_A_TIME,
        text=(
            f"Looks like {_meals(resolution.meals_visible)} in that photo — I "
            f"build one order at a time, so I'd get it wrong if I guessed. Send "
            f"me a photo of just the one you want, or tell me which it is and "
            f"I'll build it from there."
        ),
        meals_visible=resolution.meals_visible,
    )


# --- the sentences ----------------------------------------------------------


def _composition(items: Sequence[ResolvedItem]) -> str:
    """Describe a composition of catalogue rows: the entree, then what is in it.

    Every word of the result came off a row. The first item is the entree,
    which is the order stage 5 builds both a draft and an offer in.
    """
    if not items:
        return "nothing"
    entree, *rest = items
    if not rest:
        return f"a {entree.name}"
    return f"a {entree.name} with {_listed([item.name for item in rest])}"


def _believed(seen: Sequence[SeenSlot], *, without: set[Slot]) -> list[str]:
    """State what was made out, leaving out anything about to be asked about.

    The exclusion is what keeps the turn from contradicting itself. A term above
    its floor that resolves to no catalogue row is both *believed* and
    *asked about*, and a reply that said "I can see Steak" before asking what
    the protein was would read as not having looked at the photograph.
    """
    named = [slot for slot in seen if slot.slot not in without]
    if not named:
        return []
    vessel = next((slot for slot in named if slot.slot is Slot.VESSEL), None)
    rest = [slot.name for slot in named if slot.slot is not Slot.VESSEL]
    if vessel is None:
        return [f"I can see {_listed(rest)}."]
    if not rest:
        return [f"It looks like a {vessel.name}."]
    return [f"It looks like a {vessel.name} with {_listed(rest)}."]


def _asks(clarifications: Sequence[Clarification]) -> list[str]:
    """Ask about every slot that could not be believed, naming each one.

    Grouped by reason and in a fixed order, because the three reasons are three
    different questions and only one of them can be asked in the plural. A term
    the menu does not carry is raised first: it is the most actionable of the
    three, since the visitor has something specific to replace. A term that was
    half-seen comes next. A slot that was not seen at all comes last, because
    it is the one carrying no information for them to react to.
    """
    by_reason = {
        reason: [item for item in clarifications if item.reason is reason]
        for reason in (
            ClarificationReason.NO_CATALOGUE_ROW,
            ClarificationReason.LOW_CONFIDENCE,
            ClarificationReason.MISSING,
        )
    }
    asked: list[str] = []
    for clarification in by_reason[ClarificationReason.NO_CATALOGUE_ROW]:
        asked.append(_no_row(clarification))
    unsure = by_reason[ClarificationReason.LOW_CONFIDENCE]
    if unsure:
        asked.append(_unsure(unsure))
    missing = by_reason[ClarificationReason.MISSING]
    if missing:
        asked.append(_missing(missing))
    return asked


def _no_row(clarification: Clarification) -> str:
    """Ask about one term the menu does not carry on this entree."""
    noun = slot_noun(clarification.slot)
    if clarification.name is None:
        return f"I couldn't match {noun} to anything we make — what would you like?"
    return (
        f"I read {noun} as {clarification.name}, and that isn't one we make "
        f"here — what would you like instead?"
    )


def _unsure(clarifications: Sequence[Clarification]) -> str:
    """Say which slot was the least certain call, and what was read into it.

    The naming is the requirement. "I'm not certain about all of this" has the
    same hedging tone and tells the visitor nothing about which answer to check,
    which is the failure PRD V5's clarifying question is specified against.
    """
    nouns = _listed([slot_noun(item.slot) for item in clarifications])
    named = [item.name for item in clarifications if item.name is not None]
    lead = f"I'm least sure about {nouns}"
    if len(named) != len(clarifications):
        # One of them has no published word, so there is no honest way to say
        # what was read into all of them. Ask without quoting any: a list that
        # named some and not others would read as a shorter list.
        return f"{lead} — have I got {_them(clarifications)} right?"
    return f"{lead} — I read {_them(clarifications)} as {_listed(named)}. Is that right?"


def _missing(clarifications: Sequence[Clarification]) -> str:
    """Ask about the slots the model left out rather than filling with a guess."""
    nouns = [slot_noun(item.slot) for item in clarifications]
    if len(nouns) == 1:
        return f"I couldn't make out {nouns[0]} — what was it?"
    return f"I couldn't make out {_listed(nouns, joiner='or')} — what were they?"


# --- words ------------------------------------------------------------------


def _them(clarifications: Sequence[Clarification]) -> str:
    """``it`` or ``them``, agreeing with how many slots are being asked about."""
    return "it" if len(clarifications) == 1 else "them"


def _listed(names: Iterable[str], *, joiner: str = "and") -> str:
    """Join names the way a sentence does: commas, then a word before the last.

    Args:
        names: The names, in the order to say them.
        joiner: The word before the last one. ``or`` where the sentence is
            asking about alternatives rather than listing what is there.

    Returns:
        The joined phrase, empty for no names at all.
    """
    listed = list(names)
    if not listed:
        return ""
    if len(listed) == 1:
        return listed[0]
    return f"{', '.join(listed[:-1])} {joiner} {listed[-1]}"


_COUNT_WORDS: Final = (
    "",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)
"""Small counts as words, because "Looks like 4 meals" reads like a receipt."""


def _meals(count: int) -> str:
    """Say the count of meals, hedged for anything a glance would not be sure of.

    ``docs/decisions/multi-meal-photos.md`` requires the count itself -- "about
    four" is an observation the visitor can correct, and "several" is a category
    they cannot. The hedge appears from three upward because that is where
    counting a photograph stops being certain; two things in a frame is not an
    estimate, and calling it one would invite a correction that is not needed.
    """
    counted = _COUNT_WORDS[count] if 2 <= count <= 10 else str(count)
    return f"{counted} meals" if count <= 2 else f"about {counted} meals"
