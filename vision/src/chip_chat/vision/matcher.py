"""Stage 5. Described slots become catalogue SKUs, or become a question.

RFC-001 D3 has two halves and stage 4 is only the first of them. The model is
constrained to an ingredient vocabulary so that it *cannot* name a product; this
module is where a product is named, and it is ordinary deterministic code on
purpose. *"SKU resolution happens after inference so that no model output is
trusted as a product identifier."*

The guarantee this module owns
------------------------------

**No SKU in any response that does not exist in the catalogue.** Not unlikely --
impossible, because the only path from a described meal to a product identifier
runs through a lookup in :class:`~chip_chat.catalog.records.MenuCatalog`. There
is no fuzzy string match against model output anywhere below, no nearest-term
fallback, and no place a term the catalogue does not publish could become one it
does. A term that resolves to nothing produces a question, and a question names
no item.

The two lookups, and why neither is a string comparison against model text
--------------------------------------------------------------------------

**A vessel and a protein are each half of an entree.** ``CMG-101`` is the
Chicken Bowl: neither ``bowl`` nor ``chicken`` identifies it, and
:attr:`~chip_chat.catalog.records.VocabularyTerm.item_ids` is empty for both
slots so that no matcher can resolve "a bowl" to a SKU without ever learning
what was in it. The pair resolves through the published
``(item_type, primary_filling)`` of an orderable entree, and the two published
names come off the vocabulary rows the model's enum was generated from -- so the
term and the column are the same string by construction rather than by spelling.

**A modifier's identity is per-parent.** Chipotle publishes guacamole as
``CMG-1001`` on a burrito and ``CMG-1207`` on a single taco, at different
prices; ``docs/action-surface.md`` calls resolving it to one identifier and
reusing it "the first mistake a naive matcher makes". So
:attr:`~chip_chat.catalog.records.VocabularyTerm.item_ids` is a *candidate set*,
and the answer is the ``modifiers`` row joining that candidate to the entree the
meal actually resolved to. A candidate the entree does not offer resolves to
nothing, which is a question rather than a SKU from the wrong parent.

Thresholds are the tuning surface that replaces model confidence
----------------------------------------------------------------

D3 "moves the failure into a slot confidence we can threshold on", and PRD V5
says what to do at the bottom of that range: ask, do not guess. The floors are
per slot because the slots are not equally forgiving -- a wrong protein is a
different order, a wrong topping is the same order with something extra on it --
and they are :class:`SlotRules`, read from the environment, because issue #56
exists to tune them against thirty labeled photographs and a constant is not
tunable.

The asymmetry below the floor is deliberate:

* A **required** slot that is missing, below its floor, or resolves to no
  catalogue row becomes a :class:`Clarification`. Nothing is proposed.
* An **optional** slot in the same state is **dropped** and recorded. A topping
  the model half-saw must not arrive as an order the visitor did not want, and
  the confirmation card is editable in place, so the cheap correction is adding
  one back rather than noticing one that was never mentioned.

Two things stage 5 declines to run on at all
--------------------------------------------

``meals_visible >= 2``: RFC-001 section 07 is explicit that the count *gates*
the pipeline rather than shaping the draft -- "at two or more, stage 5 does not
run" -- because the schema returns one slot set, so on a frame with several
meals those slots describe the photograph and not any one meal. Resolving them
would produce a draft composed entirely of real catalogue items that nobody in
the picture is eating.

``is_chipotle_style`` false: the food in the frame is not the kind this
restaurant serves, so there is no honest entree for it. Both arrive here as an
:class:`Outcome` rather than an exception, because a deterministic matcher
answering "not this" is an ordinary result and not a failure.

What the visitor is *told* in either case is issue #55's and lives in
:mod:`chip_chat.vision.reply`. What this module owes that one is the material to
say it with, because the sentence has menu words in it and this is the component
allowed to produce those:

* :attr:`Resolution.alternative` -- PRD V4 requires the non-Chipotle path to
  "offer the closest thing that is available", which is a concrete composition
  of catalogue rows and therefore a lookup rather than a phrase. It is built
  here, from the same indexes and under the same rule as a draft: available at
  this restaurant, or not offered.
* :attr:`Resolution.seen` -- PRD V3 requires stating what was believed "in the
  visitor's language", and PRD V5 requires a clarifying question to name the
  slot it is unsure of. Both need the *published* word for a term rather than
  the slug the model returned, so every believed slot carries the name off its
  vocabulary row and :class:`Clarification` carries one too.

:mod:`chip_chat.vision.reply` holds no catalogue and takes none. It cannot name
an item that did not come off a row, for the same reason the matcher cannot read
the model's ``notes``: it is never given the thing.

One catalogue, one vocabulary
-----------------------------

A description carries the ``content_version`` of the build whose vocabulary
constrained the model. If it is not this catalogue's, :meth:`MealMatcher.resolve`
raises :class:`CatalogueDriftError` rather than resolving terms from one menu
against the rows of another -- which is the failure that would put a real SKU in
front of a visitor for food the photograph does not show.
"""

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from chip_chat.catalog.records import (
    MenuCatalog,
    MenuItem,
    Modifier,
    Slot,
    VocabularyTerm,
)
from chip_chat.otel import ToolName, agent_step, matcher_resolve, tool_call
from chip_chat.vision.describe import DescribedMeal, SlotValue
from chip_chat.vision.store import PHOTO_REF_ARGUMENT

__all__ = [
    "ENV_PREFIX",
    "REQUIRED_SLOTS",
    "CatalogueDriftError",
    "Clarification",
    "ClarificationReason",
    "DiscardedSlot",
    "MealMatcher",
    "Outcome",
    "Resolution",
    "ResolvedItem",
    "SeenSlot",
    "SlotRule",
    "SlotRules",
]

ENV_PREFIX: Final = "CHIP_CHAT_MATCHER_"
"""Prefix of every knob :meth:`SlotRules.from_env` reads."""

NOTHING_SEEN: Final = 0.0
"""The confidence carried by a :attr:`Resolution.alternative` item.

An offer is not an observation. Nothing in the frame was seen as a Chicken Bowl
-- the frame is a poke bowl -- so there is no model confidence to carry, and
carrying one would make an offer look like a match on any surface that charts
the number.
"""

ENTREE_CATEGORY: Final = "Entree"
"""The published category an orderable entree carries.

The same constant :mod:`chip_chat.catalog.vocabulary` generates the vessel
vocabulary from, and it is a published category rather than a food name -- the
one string this module compares against the catalogue that is not itself read
out of a catalogue row.
"""

REQUIRED_SLOTS: Final = (Slot.VESSEL, Slot.PROTEIN, Slot.RICE, Slot.BEANS)
"""Which slots a draft cannot be proposed without, by default.

``vessel`` and ``protein`` because they are the two halves of an entree and
there is no SKU without both. ``rice`` and ``beans`` because
``docs/action-surface.md`` section 1.3 reads their groups off the published menu
as ``(1, 1)`` on every burrito, bowl and salad: a bowl with no rice selection is
not an under-specified bowl but an invalid one, and the way to order one without
rice is to select the published absence rather than to omit the choice.

Configuration rather than a constant -- see :class:`SlotRules` -- because the
published grammar is per ``item_type`` and V0's slots are the burrito-and-bowl
ones.
"""

_DEFAULT_FLOORS: Final[Mapping[Slot, float]] = {
    Slot.VESSEL: 0.70,
    Slot.PROTEIN: 0.75,
    Slot.RICE: 0.55,
    Slot.BEANS: 0.55,
    Slot.SALSAS: 0.50,
    Slot.TOPPINGS: 0.50,
}
"""Starting floors, chosen by what being wrong costs rather than by symmetry.

``protein`` is the highest because a wrong protein is a different meal at a
different price, and it is the slot a visitor is most likely to send an order
back over. The vessel sits just below it: a bowl mistaken for a burrito is also
a different order, but a photograph shows the vessel plainly and a model is
rarely unsure of it, so a floor above the protein's would fire on almost nothing
and refuse the occasional legitimate photograph for it. Rice and beans are
required and *frequently* half-hidden under everything else, so a floor as high
as the protein's would ask a question about most real photographs. Salsas and
toppings are optional, so their floor decides what gets dropped rather than what
gets asked, and dropping is cheap to undo on an editable card.

These are the numbers issue #56 exists to move. They are a starting point
argued from the cost of each mistake, not measurements -- the labeled photo set
is what turns them into measurements.
"""


class Outcome(StrEnum):
    """What stage 5 concluded about one description.

    Attributes:
        RESOLVED: Every required slot landed on a catalogue row. There is a
            draft to price and propose.
        CLARIFY: At least one required slot is missing, below its floor, or
            resolves to nothing this entree offers. PRD V5: ask, do not guess.
        SEVERAL_MEALS: The frame holds two or more orderable meals, so stage 5
            did not run. PRD V7 and ``docs/decisions/multi-meal-photos.md``.
        NOT_ORDERABLE: The food in the frame is not the kind this restaurant
            serves. PRD V4; what to offer instead is issue #55's.
    """

    RESOLVED = "resolved"
    CLARIFY = "clarify"
    SEVERAL_MEALS = "several_meals"
    NOT_ORDERABLE = "not_orderable"


class ClarificationReason(StrEnum):
    """Why one slot became a question instead of an item.

    Attributes:
        MISSING: The model left the slot out, which the stage-4 prompt asks it
            to do rather than fill with the likely answer.
        LOW_CONFIDENCE: It filled the slot below that slot's floor.
        NO_CATALOGUE_ROW: It filled the slot with a term the catalogue
            publishes, and this entree does not offer it -- or, for a vessel
            and protein, the pair is not an entree the menu sells. Not a model
            error and not repairable: the nearest row is a fabricated order
            with a real SKU on it.
    """

    MISSING = "missing"
    LOW_CONFIDENCE = "low_confidence"
    NO_CATALOGUE_ROW = "no_catalogue_row"


class CatalogueDriftError(RuntimeError):
    """The description was constrained by a different catalogue build than this.

    A build fault rather than anything a visitor did: the vocabulary module and
    the loaded catalogue came from two harvests. Resolving across them would
    turn a term the model meant one way into a row that means another, so it
    raises here instead -- loudly, and before anything is proposed.
    """


@dataclass(frozen=True, slots=True)
class SlotRule:
    """One slot's floor, and whether a draft can be proposed without it.

    Attributes:
        floor: The lowest confidence this slot may be believed at. A value
            *below* it never becomes an item.
        required: Whether falling below the floor -- or being absent, or
            resolving to no catalogue row -- escalates to a question. An
            optional slot in the same state is dropped instead.

            ``vessel`` and ``protein`` are the exception, and setting either to
            ``False`` does not make a draft possible without it: the two are
            the halves of one entree and there is no SKU to propose from one of
            them. What the flag still controls there is the *floor*, which is
            checked either way. It is a knob on the other four.
    """

    floor: float
    required: bool

    def __post_init__(self) -> None:
        """Refuse a floor that is not a probability.

        Raises:
            ValueError: If ``floor`` is outside ``[0, 1]``. A floor above one
                would refuse every photograph and a negative one would accept
                anything the model said, which is the guess this design exists
                to remove.
        """
        if not 0.0 <= self.floor <= 1.0:
            raise ValueError(f"a slot floor must be in [0, 1], got {self.floor}")


@dataclass(frozen=True, slots=True)
class SlotRules:
    """The per-slot thresholds, as configuration rather than as constants.

    Issue #54's third acceptance criterion, and the reason it is an acceptance
    criterion: the floors are the tuning surface that replaces model confidence,
    and a number tuned against thirty labeled photographs has to be settable
    without a code change.

    Attributes:
        rules: One :class:`SlotRule` per slot. Every slot the stage-4 schema
            defines has an entry; :meth:`for_slot` raises rather than inventing
            a default for one that does not, because an unchosen threshold must
            not quietly become a permissive one.
    """

    rules: Mapping[Slot, SlotRule]

    @classmethod
    def defaults(cls) -> "SlotRules":
        """Return the starting floors of :data:`_DEFAULT_FLOORS`."""
        return cls(
            rules={
                slot: SlotRule(floor=floor, required=slot in REQUIRED_SLOTS)
                for slot, floor in _DEFAULT_FLOORS.items()
            }
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SlotRules":
        """Build the rules from the environment, one slot at a time.

        Reads ``CHIP_CHAT_MATCHER_<SLOT>_THRESHOLD`` and
        ``CHIP_CHAT_MATCHER_<SLOT>_REQUIRED`` for every slot, e.g.
        ``CHIP_CHAT_MATCHER_PROTEIN_THRESHOLD``. Every one is optional and
        falls back to :meth:`defaults`, so an unset environment is the argued
        starting point rather than an unthresholded matcher.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The configured rules.

        Raises:
            ValueError: If a floor does not parse as a number in ``[0, 1]``, or
                a required flag is not a boolean word. Failing at startup is the
                point: a misspelled threshold that silently kept the default
                would be a tuning run that measured the wrong number.
        """
        source = os.environ if env is None else env
        fallback = cls.defaults()
        return cls(
            rules={
                slot: SlotRule(
                    floor=_float(source, slot, "THRESHOLD", rule.floor),
                    required=_bool(source, slot, "REQUIRED", rule.required),
                )
                for slot, rule in fallback.rules.items()
            }
        )

    def for_slot(self, slot: Slot) -> SlotRule:
        """Return one slot's rule.

        Args:
            slot: The slot.

        Returns:
            Its floor and whether it is required.

        Raises:
            KeyError: If no rule was configured for it.
        """
        try:
            return self.rules[slot]
        except KeyError:
            raise KeyError(
                f"no threshold is configured for the {slot.value} slot"
            ) from None

    @property
    def required(self) -> tuple[Slot, ...]:
        """Every slot a draft cannot be proposed without, in schema order."""
        return tuple(slot for slot in Slot if self.rules.get(slot, _OPTIONAL).required)


_OPTIONAL: Final = SlotRule(floor=1.0, required=False)
"""Stands in for an unconfigured slot in :attr:`SlotRules.required` only.

Never used to *accept* anything: :meth:`SlotRules.for_slot` raises on a slot
with no rule, so a floor of 1.0 here cannot become a slot resolved without one.
"""


@dataclass(frozen=True, slots=True)
class ResolvedItem:
    """One catalogue row a described slot landed on.

    Every field but :attr:`confidence` is read off the catalogue. There is no
    constructor here that takes model text, which is what makes "every resolved
    item is a real catalogue row" a property of the type rather than a
    convention.

    Attributes:
        slot: Which slot produced it.
        term: The vocabulary term the model returned. Carried for the span and
            for a card that wants to say what was seen; it is not an identifier.
        item_id: The published catalogue identifier. **A real row, always.**
        name: The published name of that row -- the only place in this lane a
            menu word may come from.
        confidence: What the model said about the term, above this slot's floor
            -- and :data:`NOTHING_SEEN` on a :attr:`Resolution.alternative`
            item, where the photograph showed nothing that this row was chosen
            from. A number the model never produced must not read as one it did.
        modifier_id: For a modifier, the ``<item_id>:<modifier_item_id>`` pair
            that identifies it *on this entree*, since the same ingredient is a
            different modifier on a different parent. ``None`` for the entree.
        unit_price: What the restaurant charges, or ``None`` where no price row
            exists for it. ``None`` is not zero and must never be defaulted to
            zero on the way to a total.
        available: Whether the restaurant had it at harvest time. ``False`` also
            for an item this restaurant published no price row for at all --
            the fail-closed reading, since the alternative is telling a visitor
            a restaurant stocks something nobody said it stocks. The two cases
            are distinguishable: only the second has a null
            :attr:`unit_price`.
    """

    slot: Slot
    term: str
    item_id: str
    name: str
    confidence: float
    modifier_id: str | None = None
    unit_price: Decimal | None = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class SeenSlot:
    """One slot the matcher believed, in the word the catalogue publishes.

    The difference from :class:`ResolvedItem` is what it is *for*. A resolved
    item is a row on a draft and has an identifier and a price; a seen slot is
    an observation to read back to the visitor, and a vessel has no identifier
    of its own to be one. PRD V3 asks for what was believed to be stated "in the
    visitor's language" before they confirm, and that is every believed slot
    rather than only the ones that became SKUs.

    Attributes:
        slot: Which slot it is.
        term: The vocabulary term the model returned, e.g. ``white_rice``.
        name: The published name that term was derived from, e.g.
            ``White Rice``. **Read off the vocabulary row**, which is what makes
            a sentence built from it unable to name something unpublished.
        confidence: What the model said, at or above this slot's floor.
    """

    slot: Slot
    term: str
    name: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Clarification:
    """One required slot that has to be asked about rather than guessed.

    Attributes:
        slot: The slot in question.
        reason: Why it could not be resolved. See :class:`ClarificationReason`.
        term: What the model returned, where it returned anything. ``None`` for
            :attr:`ClarificationReason.MISSING`.
        name: The published name of :attr:`term`, where the vocabulary carries
            one. The question is asked in this word rather than in the slug,
            because PRD V5's clarifying question is read by a visitor -- and it
            comes off a catalogue row rather than out of a phrase book, so a
            question about a term cannot name a thing the menu does not.
        confidence: What it said about that term, where it said anything.
    """

    slot: Slot
    reason: ClarificationReason
    term: str | None = None
    name: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class DiscardedSlot:
    """One optional slot that was not believed, and so is not on the draft.

    Recorded rather than merely omitted: an operator retuning a floor needs to
    see what that floor threw away, and a confirmation card may reasonably say
    "I wasn't sure about this" next to an item it did not add.

    Attributes:
        slot: The slot.
        reason: Why it was dropped. Never :attr:`ClarificationReason.MISSING` --
            an optional slot the model left out is simply absent.
        term: The term the model returned.
        confidence: What it said about it.
    """

    slot: Slot
    reason: ClarificationReason
    term: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Resolution:
    """What stage 5 made of one described meal.

    Attributes:
        outcome: See :class:`Outcome`.
        restaurant_id: Whose prices :attr:`total` is in. Money is per restaurant
            because Chipotle's really is -- see
            ``docs/decisions/menu-pricing.md``.
        content_version: The catalogue build this was resolved against.
        entree: The entree row, or ``None`` on any outcome but
            :attr:`Outcome.RESOLVED`.
        modifiers: The modifier rows, in schema order.
        clarifications: The required slots to ask about. Non-empty exactly when
            :attr:`outcome` is :attr:`Outcome.CLARIFY`.
        discarded: The optional slots that fell below their floor.
        meals_visible: What the model counted, carried through so that a caller
            handling :attr:`Outcome.SEVERAL_MEALS` can say how many it saw --
            PRD V7 requires saying the number rather than picking one.
        seen: Every slot the matcher believed, in published words, in schema
            order. Populated whenever stage 5 ran -- on
            :attr:`Outcome.RESOLVED` and on :attr:`Outcome.CLARIFY`, where it is
            what lets the question say what it *did* make out rather than only
            what it did not. Empty on the two outcomes stage 5 declines to run
            on, because nothing was believed on either.
        alternative: The closest available meal this restaurant sells, on
            :attr:`Outcome.NOT_ORDERABLE` and empty on every other outcome. The
            entree first, then one row for each required modifier slot -- the
            same composition a draft has, and every row a real one. Empty also
            when the restaurant has no available entree at all, which is a
            catalogue state rather than a photograph and is why a caller has to
            handle it. **Not a draft**: it was chosen from the menu rather than
            resolved from the photograph, so it is not in :meth:`items`, not in
            :meth:`item_ids`, and not in :meth:`total`.
    """

    outcome: Outcome
    restaurant_id: int
    content_version: str | None = None
    entree: ResolvedItem | None = None
    modifiers: tuple[ResolvedItem, ...] = ()
    clarifications: tuple[Clarification, ...] = ()
    discarded: tuple[DiscardedSlot, ...] = ()
    meals_visible: int = 0
    seen: tuple[SeenSlot, ...] = ()
    alternative: tuple[ResolvedItem, ...] = ()

    @property
    def resolved(self) -> bool:
        """Whether there is a draft to propose."""
        return self.outcome is Outcome.RESOLVED

    @property
    def escalates(self) -> bool:
        """Whether this turn asks a question instead of proposing an order."""
        return self.outcome is Outcome.CLARIFY

    def items(self) -> tuple[ResolvedItem, ...]:
        """The entree and its modifiers, entree first."""
        return ((self.entree,) if self.entree is not None else ()) + self.modifiers

    def item_ids(self) -> tuple[str, ...]:
        """Every catalogue identifier on this draft, entree first.

        What the ``matcher.resolve`` span records, and what a test asserting
        "no SKU that does not exist" asserts against.
        """
        return tuple(item.item_id for item in self.items())

    def total(self) -> Decimal | None:
        """What this draft costs at :attr:`restaurant_id`.

        Returns:
            The sum of every resolved item's price, or ``None`` if any of them
            has no price row at this restaurant. ``None`` rather than a partial
            sum: a total missing one line is a wrong number that looks like a
            right one, and quoting it is worse than declining to quote.
        """
        items = self.items()
        if not items:
            return None
        running = Decimal(0)
        for item in items:
            if item.unit_price is None:
                return None
            running += item.unit_price
        return running

    def unavailable(self) -> tuple[ResolvedItem, ...]:
        """Resolved items this restaurant did not have when it was harvested."""
        return tuple(item for item in self.items() if not item.available)


class MealMatcher:
    """Stage 5. One instance per process is enough.

    Holds a catalogue and the floors, and nothing else -- no client, no
    deployment, no network. That is the point of D3's second half: the step
    that names a product is the step with no model in it.
    """

    __slots__ = (
        "_by_item",
        "_catalog",
        "_entrees",
        "_modifiers",
        "_names",
        "_prices",
        "_rules",
    )

    def __init__(
        self,
        catalog: MenuCatalog,
        *,
        rules: SlotRules | None = None,
    ) -> None:
        """Assemble the matcher.

        Args:
            catalog: The built catalogue. Required and without a default, for
                the same reason
                :class:`~chip_chat.vision.describe.MealDescriber` requires a
                vocabulary: a matcher that could be built without one would be
                a matcher resolving against nothing.
            rules: The per-slot floors. Defaults to :meth:`SlotRules.defaults`,
                which is the argued starting point; a deployment tuning them
                passes :meth:`SlotRules.from_env`.
        """
        self._catalog = catalog
        self._rules = SlotRules.defaults() if rules is None else rules
        self._entrees = _entree_index(catalog)
        self._modifiers = _modifier_index(catalog.modifiers)
        self._prices = _price_index(catalog)
        self._names = _name_index(catalog)
        self._by_item = {item.item_id: item for item in catalog.menu_items}

    @property
    def catalog(self) -> MenuCatalog:
        """The catalogue in force, for an ops surface that wants to report it."""
        return self._catalog

    @property
    def rules(self) -> SlotRules:
        """The floors in force."""
        return self._rules

    def resolve(
        self,
        meal: DescribedMeal,
        *,
        restaurant_id: int | None = None,
        content_version: str | None = None,
    ) -> Resolution:
        """Resolve one described meal to catalogue rows.

        Emits ``matcher.resolve``, which RFC-001 section 09 places under
        ``tool.<tool_name>`` -- so this runs inside a tool call. Use
        :meth:`resolve_as_tool` if there is not one already open.

        Args:
            meal: The stage-4 description. A
                :class:`~chip_chat.vision.describe.DescribedMeal` and never a
                :class:`~chip_chat.vision.describe.Description`, because the
                latter carries the model's one free-text field and this is the
                component that must not be able to read it.
            restaurant_id: Whose prices to quote. Defaults to the catalogue's
                reference restaurant.
            content_version: The catalogue build whose vocabulary constrained
                the model -- ``Description.content_version``. Checked when
                given; when it is ``None`` there is nothing to check, which is
                the case for a vocabulary module whose docstring carried no
                version.

        Returns:
            The :class:`Resolution`. Its :attr:`~Resolution.item_ids` are
            catalogue identifiers or it has none.

        Raises:
            CatalogueDriftError: If ``content_version`` is not this catalogue's.
        """
        self._require_same_catalogue(content_version)
        restaurant = (
            self._catalog.reference_restaurant_id
            if restaurant_id is None
            else restaurant_id
        )
        version = self._catalog.content_version()

        with matcher_resolve() as recorder:
            recorder.record_slots(_span_slots(meal))
            resolution = self._resolve(meal, restaurant, version)
            recorder.record_resolved_skus(resolution.item_ids())
            if resolution.escalates:
                recorder.record_escalation(_escalation_reason(resolution))
        return resolution

    def resolve_as_tool(
        self,
        meal: DescribedMeal,
        *,
        restaurant_id: int | None = None,
        content_version: str | None = None,
        image_ref: str | None = None,
        step: int = 0,
    ) -> Resolution:
        """Resolve one meal, opening the spans above ``matcher.resolve`` as well.

        The counterpart of
        :meth:`~chip_chat.vision.describe.MealDescriber.describe_as_tool`, and
        for the same callers: a batch evaluation over the labeled photo set
        (issue #56), or a script. When the agent calls stage 5 the two spans
        above are already open and :meth:`resolve` is the entry point.

        Args:
            meal: The stage-4 description.
            restaurant_id: Whose prices to quote.
            content_version: The build the description was constrained by.
            image_ref: The photograph the tool was called on, for the tool
                span's arguments -- the same ``str(blob_ref)`` stage 4 records
                and, as there, a reference rather than bytes. Omitted when
                there is no photograph behind the description, which is the
                case for a matcher test and for a described meal typed by a
                visitor.
            step: The ``agent.step`` index to record.

        Returns:
            The :class:`Resolution`.

        Raises:
            CatalogueDriftError: As :meth:`resolve`.
        """
        arguments = {} if image_ref is None else {PHOTO_REF_ARGUMENT: image_ref}
        with (
            agent_step(index=step),
            tool_call(ToolName.MATCH_MEAL_FROM_PHOTO, arguments=arguments),
        ):
            return self.resolve(
                meal, restaurant_id=restaurant_id, content_version=content_version
            )

    # --- the resolution itself ---------------------------------------------

    def _resolve(self, meal: DescribedMeal, restaurant: int, version: str) -> Resolution:
        """Decide the outcome and, where there is one, build the draft."""
        declined = _declined(meal)
        if declined is not None:
            return Resolution(
                outcome=declined,
                restaurant_id=restaurant,
                content_version=version,
                meals_visible=meal.meals_visible,
                # Only on the not-orderable branch. A frame with four poke bowls
                # is both outcomes and comes back as the other one, and offering
                # an alternative there would answer a question -- which meal? --
                # that PRD V7 requires asking rather than answering.
                alternative=(
                    self._alternative(meal, restaurant)
                    if declined is Outcome.NOT_ORDERABLE
                    else ()
                ),
            )

        clarifications: list[Clarification] = []
        discarded: list[DiscardedSlot] = []

        entree = self._entree(meal, restaurant, clarifications)
        modifiers: list[ResolvedItem] = []
        for slot, value in _modifier_slots(meal):
            self._modifier(
                slot, value, entree, restaurant, modifiers, clarifications, discarded
            )
        # A required modifier slot the model omitted is a question, and the loop
        # above never sees it -- an absent slot has no value to iterate over.
        clarifications.extend(self._missing(meal))

        seen = self._seen(meal)
        if clarifications or entree is None:
            return Resolution(
                outcome=Outcome.CLARIFY,
                restaurant_id=restaurant,
                content_version=version,
                clarifications=tuple(clarifications),
                discarded=tuple(discarded),
                meals_visible=meal.meals_visible,
                seen=seen,
            )
        return Resolution(
            outcome=Outcome.RESOLVED,
            restaurant_id=restaurant,
            content_version=version,
            entree=entree,
            modifiers=tuple(modifiers),
            discarded=tuple(discarded),
            meals_visible=meal.meals_visible,
            seen=seen,
        )

    def _entree(
        self,
        meal: DescribedMeal,
        restaurant: int,
        clarifications: list[Clarification],
    ) -> ResolvedItem | None:
        """Resolve the vessel and the protein together, or ask about them.

        Together, because neither half is a SKU. A confident bowl beside an
        unreadable protein is not most of an order; it is an order whose most
        expensive decision nobody has made.
        """
        believed: dict[Slot, SlotValue] = {}
        for slot in (Slot.VESSEL, Slot.PROTEIN):
            value = _single(meal, slot)
            problem = self._believe(slot, value)
            if problem is not None or value is None:
                clarifications.append(
                    problem
                    if problem is not None
                    else Clarification(slot=slot, reason=ClarificationReason.MISSING)
                )
                continue
            believed[slot] = value
        vessel = believed.get(Slot.VESSEL)
        protein = believed.get(Slot.PROTEIN)
        if vessel is None or protein is None:
            return None

        item = self._entrees.get((vessel.value, protein.value))
        if item is None:
            # Two real terms and no real row: the menu sells a Chicken Bowl and
            # a Steak Burrito and a described steak bowl is neither. Refusing is
            # the whole of D3 -- the nearest entree is a fabricated order.
            #
            # It is the *pair* that failed, so which half to ask about is a
            # choice. Ask about the one the model was less sure of: it is the
            # likelier mistake, and asking about the confident half reads to a
            # visitor as not having looked at the photograph.
            asked = min(
                ((Slot.VESSEL, vessel), (Slot.PROTEIN, protein)),
                key=lambda pair: pair[1].confidence,
            )
            clarifications.append(
                Clarification(
                    slot=asked[0],
                    reason=ClarificationReason.NO_CATALOGUE_ROW,
                    term=asked[1].value,
                    name=self._published(asked[0], asked[1].value),
                    confidence=asked[1].confidence,
                )
            )
            return None
        price = self._prices.get((restaurant, item.item_id))
        return ResolvedItem(
            slot=Slot.VESSEL,
            term=vessel.value,
            item_id=item.item_id,
            name=item.name,
            confidence=min(vessel.confidence, protein.confidence),
            unit_price=price[0] if price is not None else None,
            available=price[1] if price is not None else False,
        )

    def _modifier(
        self,
        slot: Slot,
        value: SlotValue,
        entree: ResolvedItem | None,
        restaurant: int,
        resolved: list[ResolvedItem],
        clarifications: list[Clarification],
        discarded: list[DiscardedSlot],
    ) -> None:
        """Resolve one filled modifier slot onto the entree, or set it aside."""
        problem = self._believe(slot, value)
        if problem is None and entree is not None:
            modifier = self._offered(entree.item_id, slot, value.value)
            if modifier is None:
                problem = Clarification(
                    slot=slot,
                    reason=ClarificationReason.NO_CATALOGUE_ROW,
                    term=value.value,
                    name=self._published(slot, value.value),
                    confidence=value.confidence,
                )
            else:
                price = self._prices.get((restaurant, modifier.modifier_item_id))
                resolved.append(
                    ResolvedItem(
                        slot=slot,
                        term=value.value,
                        item_id=modifier.modifier_item_id,
                        name=modifier.name,
                        confidence=value.confidence,
                        modifier_id=modifier.modifier_id,
                        unit_price=price[0] if price is not None else None,
                        available=price[1] if price is not None else False,
                    )
                )
                return
        if problem is None:
            # There is no entree to hang it on, and the missing entree is
            # already a clarification. Nothing more to say about this slot.
            return
        if self._rules.for_slot(slot).required:
            clarifications.append(problem)
            return
        discarded.append(
            DiscardedSlot(
                slot=slot,
                reason=problem.reason,
                term=value.value,
                confidence=value.confidence,
            )
        )

    def _missing(self, meal: DescribedMeal) -> Iterator[Clarification]:
        """Yield a question for every required slot the model left empty."""
        filled = {slot for slot, _ in _filled(meal)}
        for slot in self._rules.required:
            if slot in filled or slot in (Slot.VESSEL, Slot.PROTEIN):
                # The entree pair reports its own absence, once, in _entree.
                continue
            yield Clarification(slot=slot, reason=ClarificationReason.MISSING)

    def _believe(self, slot: Slot, value: SlotValue | None) -> Clarification | None:
        """Return why ``value`` cannot be believed, or ``None`` if it can."""
        if value is None:
            return Clarification(slot=slot, reason=ClarificationReason.MISSING)
        if value.confidence < self._rules.for_slot(slot).floor:
            return Clarification(
                slot=slot,
                reason=ClarificationReason.LOW_CONFIDENCE,
                term=value.value,
                name=self._published(slot, value.value),
                confidence=value.confidence,
            )
        return None

    # --- what the visitor is told about (issue #55) -------------------------

    def _seen(self, meal: DescribedMeal) -> tuple[SeenSlot, ...]:
        """Every slot believed at or above its floor, in published words.

        Believed and not resolved: a vessel is half of an entree and never a row
        of its own, so a description whose protein could not be read has no
        resolved item to say "it looked like a bowl" with. PRD V3 asks for that
        sentence anyway, and this is what it is built from.

        A term the vocabulary does not carry is skipped rather than passed
        through. It cannot arrive from a deployment -- stage 4 validates against
        the same vocabulary -- but the only word available for one would be the
        model's own slug, and putting that in front of a visitor is the one
        thing this lane does not do.
        """
        believed: list[SeenSlot] = []
        for slot, value in _filled(meal):
            if value.confidence < self._rules.for_slot(slot).floor:
                continue
            name = self._published(slot, value.value)
            if name is None:
                continue
            believed.append(
                SeenSlot(
                    slot=slot,
                    term=value.value,
                    name=name,
                    confidence=value.confidence,
                )
            )
        return tuple(believed)

    def _alternative(
        self, meal: DescribedMeal, restaurant: int
    ) -> tuple[ResolvedItem, ...]:
        """Compose the closest available meal to what the frame showed.

        PRD V4: *"When the photo is not Chipotle-style food, says so and offers
        the closest thing that is available."* Three words in that sentence are
        doing work, and each is a rule here.

        **Closest.** A photograph of a poke bowl is still a photograph of a
        bowl, and the model fills what slots it can from the catalogue's
        vocabulary whatever the food turns out to be. Every believed slot is
        honoured where the menu can honour it, so the offer starts from what the
        visitor is actually looking at rather than from a house favourite.

        **Available.** An offer is a promise about today, so a row with no price
        at this restaurant, or one the harvest recorded as out, is not offered.
        This is the same fail-closed reading
        :attr:`ResolvedItem.available` takes, applied before the sentence rather
        than after it.

        **Thing, singular.** The offer is the composition a draft cannot be
        proposed without -- the entree and each required modifier slot -- and no
        more. Salsas and toppings the frame may have shown are left off: the
        confirmation card is editable, and the cheap correction is adding one
        rather than noticing something nobody offered.

        Ties break toward the cheapest, then toward the lower identifier. Cheap
        rather than popular because this is a suggestion the visitor did not ask
        for, and the version of that which costs more is an upsell wearing a
        helpful sentence.
        """
        believed: dict[Slot, SlotValue] = {}
        for slot in (Slot.VESSEL, Slot.PROTEIN, Slot.RICE, Slot.BEANS):
            value = _single(meal, slot)
            if value is not None and value.confidence >= self._rules.for_slot(slot).floor:
                believed[slot] = value

        entree = self._closest_entree(believed, restaurant)
        if entree is None:
            return ()
        offered = [entree]
        for slot in self._rules.required:
            if slot in (Slot.VESSEL, Slot.PROTEIN):
                continue
            modifier = self._closest_modifier(
                entree.item_id, slot, believed.get(slot), restaurant
            )
            if modifier is not None:
                offered.append(modifier)
        return tuple(offered)

    def _closest_entree(
        self, believed: Mapping[Slot, SlotValue], restaurant: int
    ) -> ResolvedItem | None:
        """Pick the available entree nearest to what was believed.

        The vessel counts for more than the protein, and deliberately: the
        vessel is the shape of the thing in the frame and it is what "closest"
        means to somebody looking at their own photograph. A protein is a
        judgement about what is inside it, which on food this menu does not
        serve is a judgement about something that has no answer here anyway.
        """
        vessel = believed.get(Slot.VESSEL)
        protein = believed.get(Slot.PROTEIN)
        best: tuple[tuple[int, Decimal, str], ResolvedItem] | None = None
        for (vessel_term, protein_term), item in self._entrees.items():
            price = self._prices.get((restaurant, item.item_id))
            if price is None or not price[1]:
                continue
            closeness = 0
            if vessel is not None and vessel.value == vessel_term:
                closeness += 2
            if protein is not None and protein.value == protein_term:
                closeness += 1
            key = (-closeness, price[0], item.item_id)
            if best is not None and key >= best[0]:
                continue
            best = (
                key,
                ResolvedItem(
                    slot=Slot.VESSEL,
                    term=vessel_term,
                    item_id=item.item_id,
                    name=item.name,
                    confidence=NOTHING_SEEN,
                    unit_price=price[0],
                    available=True,
                ),
            )
        return None if best is None else best[1]

    def _closest_modifier(
        self,
        item_id: str,
        slot: Slot,
        believed: SlotValue | None,
        restaurant: int,
    ) -> ResolvedItem | None:
        """Pick the available modifier this entree offers, nearest what was seen.

        Candidates come from the vocabulary rather than from the modifier table,
        so the offer can only be composed of things the described-meal
        vocabulary knows -- which is the same set the model could have named,
        and therefore the same set the visitor can correct it to.
        """
        offered: list[tuple[str, Modifier, Decimal]] = []
        for row in self._catalog.vocabulary:
            if row.slot is not slot:
                continue
            modifier = self._offered(item_id, slot, row.value)
            if modifier is None:
                continue
            priced = self._prices.get((restaurant, modifier.modifier_item_id))
            if priced is None or not priced[1]:
                continue
            offered.append((row.value, modifier, priced[0]))
        if not offered:
            return None
        chosen = min(offered, key=lambda row: (row[2], row[1].modifier_item_id))
        if believed is not None:
            for candidate in offered:
                if candidate[0] == believed.value:
                    chosen = candidate
                    break
        term, modifier, price = chosen
        return ResolvedItem(
            slot=slot,
            term=term,
            item_id=modifier.modifier_item_id,
            name=modifier.name,
            confidence=NOTHING_SEEN,
            modifier_id=modifier.modifier_id,
            unit_price=price,
            available=True,
        )

    def _published(self, slot: Slot, term: str) -> str | None:
        """Return the published name of one vocabulary term, if it has one."""
        return self._names.get((slot, term))

    def _offered(self, item_id: str, slot: Slot, term: str) -> Modifier | None:
        """Return the modifier row this entree offers for ``term`` in ``slot``.

        The candidate set comes from the vocabulary and the answer comes from
        the ``modifiers`` table, which is the join that keeps a burrito's
        guacamole from being priced as a taco's.
        """
        for candidate in self._catalog_candidates(slot, term):
            modifier = self._modifiers.get((item_id, candidate))
            if modifier is not None and modifier.slot is slot:
                return modifier
        return None

    def _catalog_candidates(self, slot: Slot, term: str) -> tuple[str, ...]:
        """Every catalogue item ``term`` may mean in ``slot``, from the vocabulary."""
        for row in self._catalog.vocabulary:
            if row.slot is slot and row.value == term:
                return row.item_ids
        return ()

    def _require_same_catalogue(self, content_version: str | None) -> None:
        """Refuse to resolve a description constrained by another build."""
        if content_version is None:
            return
        actual = self._catalog.content_version()
        if content_version != actual:
            raise CatalogueDriftError(
                f"the description was constrained by catalogue {content_version} "
                f"and this matcher holds {actual}; regenerate the vocabulary from "
                f"the catalogue this matcher was built with"
            )


# --- indexes ----------------------------------------------------------------
#
# Built once per matcher. Each is keyed by exactly the lookup one stage-5 step
# makes, so no step scans a table -- and, more usefully, so each lookup reads as
# the sentence it implements rather than as a comprehension.


def _entree_index(catalog: MenuCatalog) -> Mapping[tuple[str, str], MenuItem]:
    """Map ``(vessel term, protein term)`` to the entree the menu sells.

    The terms come from the vocabulary rows the model's enum was generated from,
    and each carries the published name it was derived from -- so this joins the
    model's word to the catalogue's column without either being written here.
    """
    vessels = _terms(catalog.vocabulary, Slot.VESSEL)
    proteins = _terms(catalog.vocabulary, Slot.PROTEIN)
    by_name = {
        (item.item_type, item.primary_filling): item
        for item in catalog.menu_items
        if item.category == ENTREE_CATEGORY and item.primary_filling is not None
    }
    index: dict[tuple[str, str], MenuItem] = {}
    for vessel, vessel_name in vessels.items():
        for protein, protein_name in proteins.items():
            item = by_name.get((vessel_name, protein_name))
            if item is not None:
                index[(vessel, protein)] = item
    return index


def _terms(vocabulary: Sequence[VocabularyTerm], slot: Slot) -> Mapping[str, str]:
    """Map one slot's terms to the published names they were derived from."""
    return {row.value: row.name for row in vocabulary if row.slot is slot}


def _modifier_index(
    modifiers: Sequence[Modifier],
) -> Mapping[tuple[str, str], Modifier]:
    """Map ``(entree item id, modifier item id)`` to the row joining them."""
    return {
        (modifier.item_id, modifier.modifier_item_id): modifier for modifier in modifiers
    }


def _name_index(catalog: MenuCatalog) -> Mapping[tuple[Slot, str], str]:
    """Map ``(slot, term)`` to the published name the term was derived from.

    The one place a vocabulary term becomes a word a visitor reads. It is an
    index off ``catalog.vocabulary`` rather than a table in this file for the
    reason the enums are generated: a spelling written here would be a
    hand-maintained menu in the file nobody would think to regenerate.
    """
    return {(row.slot, row.value): row.name for row in catalog.vocabulary}


def _price_index(catalog: MenuCatalog) -> Mapping[tuple[int, str], tuple[Decimal, bool]]:
    """Map ``(restaurant, item)`` to its in-store price and availability."""
    return {
        (row.restaurant_id, row.item_id): (row.unit_price, row.is_available)
        for row in catalog.item_prices
    }


# --- reading a description --------------------------------------------------


def _declined(meal: DescribedMeal) -> Outcome | None:
    """Return the outcome stage 5 declines with, or ``None`` to run.

    Order matters only in that both answers are correct and one has to be
    returned: a frame with four poke bowls in it is several meals *and* not
    orderable. The count is checked first because it is the gate RFC-001
    section 07 puts on the pipeline.
    """
    if meal.several_meals:
        return Outcome.SEVERAL_MEALS
    if not meal.is_chipotle_style:
        return Outcome.NOT_ORDERABLE
    return None


def _single(meal: DescribedMeal, slot: Slot) -> SlotValue | None:
    """Read one single-valued slot off the description."""
    value: SlotValue | None = getattr(meal, slot.value)
    return value


def _filled(meal: DescribedMeal) -> tuple[tuple[Slot, SlotValue], ...]:
    """Every filled slot as ``(slot, value)``, in schema order."""
    return tuple((Slot(name), value) for name, value in meal.slots())


def _modifier_slots(meal: DescribedMeal) -> Iterator[tuple[Slot, SlotValue]]:
    """Every filled slot that is not half of the entree."""
    for slot, value in _filled(meal):
        if slot not in (Slot.VESSEL, Slot.PROTEIN):
            yield slot, value


def _span_slots(meal: DescribedMeal) -> Mapping[str, tuple[str, float]]:
    """What the model said, keyed for ``matcher.resolve``.

    A multi-valued slot contributes one indexed key per value, so the two lists
    the recorder writes stay aligned and a dashboard can chart one topping's
    confidence without parsing JSON. Model *terms*, not resolved SKUs -- the
    SKUs are the other attribute, and keeping them apart is what lets a trace
    show a term that resolved to nothing.
    """
    counts: dict[Slot, int] = {}
    slots: dict[str, tuple[str, float]] = {}
    for slot, value in _filled(meal):
        if slot in (Slot.SALSAS, Slot.TOPPINGS):
            index = counts.get(slot, 0)
            counts[slot] = index + 1
            slots[f"{slot.value}[{index}]"] = (value.value, value.confidence)
        else:
            slots[slot.value] = (value.value, value.confidence)
    return slots


def _escalation_reason(resolution: Resolution) -> str:
    """One line naming what has to be asked about, for the span.

    Slot names and reasons only. A visitor-facing question is issue #55's and
    is not a span attribute.
    """
    return ", ".join(
        f"{clarification.slot.value}:{clarification.reason.value}"
        for clarification in resolution.clarifications
    )


# --- configuration ----------------------------------------------------------

_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})


def _float(env: Mapping[str, str], slot: Slot, suffix: str, default: float) -> float:
    """Read one ``CHIP_CHAT_MATCHER_<SLOT>_<SUFFIX>`` floor."""
    name = f"{ENV_PREFIX}{slot.value.upper()}_{suffix}"
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} is {raw!r}, which is not a number") from None


def _bool(env: Mapping[str, str], slot: Slot, suffix: str, default: bool) -> bool:
    """Read one ``CHIP_CHAT_MATCHER_<SLOT>_<SUFFIX>`` flag."""
    name = f"{ENV_PREFIX}{slot.value.upper()}_{suffix}"
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    allowed = ", ".join(sorted(_TRUE | _FALSE))
    raise ValueError(f"{name} is {raw!r}; expected one of {allowed}")
