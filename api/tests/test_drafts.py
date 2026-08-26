"""The draft store: what can be minted, what it costs, and who may confirm it.

Issue #62's acceptance criteria are the five headings below, and each is asserted
against the committed catalogue rather than against a menu written to suit the
test. Where a rule needs a row the fixture does not have -- a default modifier, an
item a store has run out of, a second rice in one group -- the row is made by
:func:`dataclasses.replace` on a real one, so the shape under test stays the
shape the loader produces.

Nothing here waits for time to pass: the store reads the clock the fixture
drives, which is the only way the expiry cases can be asserted at all.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import Any

import pytest

from chip_chat.api.drafts import DraftRejectedError, DraftStore, OrderType, RejectionCode
from chip_chat.api.testing import FakeClock
from chip_chat.catalog import MenuCatalog

VISITOR = "demo-0001"
STRANGER = "demo-0002"

BURRITO = "CMG-2"
BOWL = "CMG-101"
CHIPS = "CMG-1002"
DRINK = "CMG-2022"
GUACAMOLE = "CMG-1001"
EXTRA_CHICKEN = "CMG-1101"
WHITE_RICE = "CMG-5001"
BLACK_BEANS = "CMG-5051"
CHEESE = "CMG-5252"

RESTAURANT = 679
"""The one restaurant the fixture catalogue prices."""


def burrito(**changes: Any) -> dict[str, Any]:
    """A composable Steak Burrito line: both required groups filled."""
    line: dict[str, Any] = {
        "item_id": BURRITO,
        "quantity": 1,
        "selections": [
            {"modifier_item_id": WHITE_RICE},
            {"modifier_item_id": BLACK_BEANS},
        ],
    }
    line.update(changes)
    return line


def price_of(catalog: MenuCatalog, item_id: str, *, delivery: bool = False) -> Decimal:
    """What the catalogue publishes for ``item_id`` at the fixture restaurant."""
    for row in catalog.item_prices:
        if row.restaurant_id == RESTAURANT and row.item_id == item_id:
            return row.unit_delivery_price if delivery else row.unit_price
    raise AssertionError(f"the fixture catalogue prices no {item_id}")


def rejection(error: pytest.ExceptionInfo[DraftRejectedError]) -> RejectionCode:
    return error.value.code


# --- prices match the catalogue exactly -------------------------------------


def test_a_draft_is_priced_from_the_catalogue(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    """The published price, not a number this module knows."""
    draft = drafts.propose(VISITOR, [burrito()])

    assert draft.total == price_of(catalog, BURRITO)


def test_quantity_multiplies_the_published_price(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    draft = drafts.propose(VISITOR, [{"item_id": CHIPS, "quantity": 3}])

    assert draft.total == price_of(catalog, CHIPS) * 3


def test_modifier_deltas_are_the_modifiers_own_published_price(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    """Extra chicken is real money; rice and beans are included."""
    draft = drafts.propose(
        VISITOR,
        [
            burrito(
                selections=[
                    {"modifier_item_id": WHITE_RICE},
                    {"modifier_item_id": BLACK_BEANS},
                    {"modifier_item_id": EXTRA_CHICKEN},
                ]
            )
        ],
    )

    assert draft.total == price_of(catalog, BURRITO) + price_of(catalog, EXTRA_CHICKEN)


def test_an_included_modifier_costs_nothing(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    plain = drafts.propose(VISITOR, [burrito()])
    with_cheese = drafts.propose(
        VISITOR,
        [
            burrito(
                selections=[
                    {"modifier_item_id": WHITE_RICE},
                    {"modifier_item_id": BLACK_BEANS},
                    {"modifier_item_id": CHEESE},
                ]
            )
        ],
    )

    assert with_cheese.total == plain.total
    assert price_of(catalog, CHEESE) == Decimal("0")


def test_a_delivery_draft_is_priced_in_the_delivery_column(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    """Section 7.1 rule 10: the two columns are never mixed on one card."""
    draft = drafts.propose(VISITOR, [burrito()], order_type=OrderType.DELIVERY)

    assert draft.total == price_of(catalog, BURRITO, delivery=True)
    assert draft.total != price_of(catalog, BURRITO)


def test_several_lines_add_up(drafts: DraftStore, catalog: MenuCatalog) -> None:
    draft = drafts.propose(
        VISITOR,
        [burrito(), {"item_id": CHIPS, "quantity": 2}, {"item_id": DRINK}],
    )

    assert draft.total == (
        price_of(catalog, BURRITO)
        + price_of(catalog, CHIPS) * 2
        + price_of(catalog, DRINK)
    )


def test_a_draft_cites_the_harvest_its_prices_came_from(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    """RFC-001 section 08: a quoted figure says when it was read."""
    draft = drafts.propose(VISITOR, [burrito()])

    used = [
        row.harvested_at
        for row in catalog.item_prices
        if row.restaurant_id == RESTAURANT
        if row.item_id in {BURRITO, WHITE_RICE, BLACK_BEANS}
    ]
    assert draft.priced_at == max(used)
    assert draft.content_version == catalog.content_version()
    assert draft.restaurant_id == RESTAURANT


# --- only real catalogue rows are mintable ----------------------------------


def test_a_nonexistent_sku_is_not_mintable(drafts: DraftStore) -> None:
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [{"item_id": "CMG-9999"}])

    assert rejection(error) is RejectionCode.ITEM_NOT_ORDERABLE
    assert len(drafts) == 0


def test_a_modifier_only_item_cannot_be_ordered_alone(drafts: DraftStore) -> None:
    """The catalogue's null category is the test for orderable-on-its-own."""
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [{"item_id": WHITE_RICE}])

    assert rejection(error) is RejectionCode.ITEM_NOT_ORDERABLE


def test_an_item_that_is_not_a_mapping_is_refused_by_name(drafts: DraftStore) -> None:
    """A model that emits a bare string gets a rule, not a stack trace.

    The ``type: ignore`` is the test: the annotation says the argument is a list
    of mappings, and what actually arrives is whatever the model emitted.
    """
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, ["a steak burrito"])  # type: ignore[list-item]

    assert rejection(error) is RejectionCode.ITEM_NOT_ORDERABLE


def test_an_empty_order_is_refused(drafts: DraftStore) -> None:
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [])

    assert rejection(error) is RejectionCode.EMPTY_ORDER


def test_a_modifier_not_published_for_this_item_is_refused(drafts: DraftStore) -> None:
    """Chips take no cheese: ``(item, modifier)`` is the published pairing."""
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(
            VISITOR,
            [{"item_id": CHIPS, "selections": [{"modifier_item_id": CHEESE}]}],
        )

    assert rejection(error) is RejectionCode.MODIFIER_NOT_OFFERED


def test_a_portion_not_published_for_this_pairing_is_refused(
    drafts: DraftStore,
) -> None:
    """Guacamole publishes ``Side`` and nothing else, so extra guac is a refusal."""
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(
            VISITOR,
            [
                burrito(
                    selections=[
                        {"modifier_item_id": WHITE_RICE},
                        {"modifier_item_id": BLACK_BEANS},
                        {"modifier_item_id": GUACAMOLE, "portion": "Extra"},
                    ]
                )
            ],
        )

    assert rejection(error) is RejectionCode.PORTION_NOT_OFFERED


def test_a_published_portion_is_kept_in_its_published_spelling(
    drafts: DraftStore,
) -> None:
    draft = drafts.propose(
        VISITOR,
        [
            burrito(
                selections=[
                    {"modifier_item_id": WHITE_RICE, "portion": "light"},
                    {"modifier_item_id": BLACK_BEANS},
                ]
            )
        ],
    )

    assert draft.lines[0].selections[0].portion == "Light"


def test_a_required_group_left_empty_is_refused(drafts: DraftStore) -> None:
    """A missing rice choice on a bowl is a rejection, not a default."""
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [{"item_id": BOWL, "selections": []}])

    assert rejection(error) is RejectionCode.REQUIRED_SLOT_EMPTY


def test_the_same_modifier_twice_on_one_line_is_refused(drafts: DraftStore) -> None:
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(
            VISITOR,
            [
                burrito(
                    selections=[
                        {"modifier_item_id": WHITE_RICE},
                        {"modifier_item_id": WHITE_RICE, "portion": "Extra"},
                        {"modifier_item_id": BLACK_BEANS},
                    ]
                )
            ],
        )

    assert rejection(error) is RejectionCode.SLOT_OVERFILLED


def test_two_choices_from_a_one_of_group_are_refused(
    catalog: MenuCatalog, clock: FakeClock
) -> None:
    """The published bounds, over a group with two members in it."""
    brown_rice = "CMG-5002"
    rice_item = next(row for row in catalog.menu_items if row.item_id == WHITE_RICE)
    rice_price = next(row for row in catalog.item_prices if row.item_id == WHITE_RICE)
    rice_modifier = next(
        row
        for row in catalog.modifiers
        if row.item_id == BURRITO and row.modifier_item_id == WHITE_RICE
    )
    widened = replace(
        catalog,
        menu_items=(
            *catalog.menu_items,
            replace(rice_item, item_id=brown_rice, name="Brown Rice"),
        ),
        item_prices=(*catalog.item_prices, replace(rice_price, item_id=brown_rice)),
        modifiers=(
            *catalog.modifiers,
            replace(
                rice_modifier,
                modifier_id=f"{BURRITO}:{brown_rice}",
                modifier_item_id=brown_rice,
                name="Brown Rice",
            ),
        ),
    )

    with pytest.raises(DraftRejectedError) as error:
        DraftStore(widened, clock=clock).propose(
            VISITOR,
            [
                burrito(
                    selections=[
                        {"modifier_item_id": WHITE_RICE},
                        {"modifier_item_id": brown_rice},
                        {"modifier_item_id": BLACK_BEANS},
                    ]
                )
            ],
        )

    assert rejection(error) is RejectionCode.SLOT_OVERFILLED


def test_an_entree_is_capped_at_one(drafts: DraftStore) -> None:
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [burrito(quantity=2)])

    assert rejection(error) is RejectionCode.QUANTITY_EXCEEDS_MAX


def test_a_side_is_capped_at_five(drafts: DraftStore) -> None:
    assert (
        drafts.propose(VISITOR, [{"item_id": CHIPS, "quantity": 5}]).lines[0].quantity
        == 5
    )

    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [{"item_id": CHIPS, "quantity": 6}])

    assert rejection(error) is RejectionCode.QUANTITY_EXCEEDS_MAX


def test_a_quantity_that_is_not_a_number_is_refused(drafts: DraftStore) -> None:
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [{"item_id": CHIPS, "quantity": "a few"}])

    assert rejection(error) is RejectionCode.QUANTITY_EXCEEDS_MAX


def test_an_item_the_store_had_run_out_of_is_refused(
    catalog: MenuCatalog, clock: FakeClock
) -> None:
    sold_out = replace(
        catalog,
        item_prices=tuple(
            replace(row, is_available=False) if row.item_id == CHIPS else row
            for row in catalog.item_prices
        ),
    )

    with pytest.raises(DraftRejectedError) as error:
        DraftStore(sold_out, clock=clock).propose(VISITOR, [{"item_id": CHIPS}])

    assert rejection(error) is RejectionCode.ITEM_UNAVAILABLE_AT_STORE


def test_an_item_that_cannot_be_delivered_is_refused_on_a_delivery_draft(
    catalog: MenuCatalog, clock: FakeClock
) -> None:
    counter_only = replace(
        catalog,
        item_prices=tuple(
            replace(row, eligible_for_delivery=False) if row.item_id == CHIPS else row
            for row in catalog.item_prices
        ),
    )
    store = DraftStore(counter_only, clock=clock)

    assert store.propose(VISITOR, [{"item_id": CHIPS}]).lines
    with pytest.raises(DraftRejectedError) as error:
        store.propose(VISITOR, [{"item_id": CHIPS}], order_type=OrderType.DELIVERY)

    assert rejection(error) is RejectionCode.NOT_ELIGIBLE_FOR_DELIVERY


def test_a_restaurant_this_catalogue_never_priced_is_refused(
    drafts: DraftStore,
) -> None:
    """Quoting the reference restaurant's prices instead would be a wrong total."""
    with pytest.raises(DraftRejectedError) as error:
        drafts.propose(VISITOR, [burrito()], restaurant_id=4_242)

    assert rejection(error) is RejectionCode.STORE_NOT_PRICED


def test_a_default_modifier_is_on_the_card_whether_or_not_it_was_named(
    catalog: MenuCatalog, clock: FakeClock
) -> None:
    """Section 7.1: a default is on the food, so it is on the card, and priced."""
    with_default = replace(
        catalog,
        modifiers=tuple(
            replace(row, is_default=True)
            if row.item_id == BURRITO and row.modifier_item_id == GUACAMOLE
            else row
            for row in catalog.modifiers
        ),
    )

    draft = DraftStore(with_default, clock=clock).propose(VISITOR, [burrito()])

    included = [
        selection
        for selection in draft.lines[0].selections
        if selection.modifier_item_id == GUACAMOLE
    ]
    assert [selection.is_default for selection in included] == [True]
    assert draft.total == price_of(catalog, BURRITO) + price_of(catalog, GUACAMOLE)


# --- confirmation is the app's to grant -------------------------------------


def test_a_new_draft_is_not_confirmed(drafts: DraftStore) -> None:
    assert drafts.propose(VISITOR, [burrito()]).confirmed is False


def test_nothing_in_the_proposal_can_mint_a_confirmed_draft(
    drafts: DraftStore,
) -> None:
    """The model may name items. It may not name the flag that gates the write."""
    draft = drafts.propose(
        VISITOR,
        [burrito(confirmed=True)],
    )

    assert draft.confirmed is False


def test_a_draft_cannot_be_confirmed_by_assignment(drafts: DraftStore) -> None:
    """Frozen, so confirmation is a store operation and never a stray attribute."""
    draft = drafts.propose(VISITOR, [burrito()])

    with pytest.raises(FrozenInstanceError):
        draft.confirmed = True  # type: ignore[misc]


def test_confirming_marks_the_draft_and_nothing_else(drafts: DraftStore) -> None:
    draft = drafts.propose(VISITOR, [burrito()])

    confirmed = drafts.confirm(VISITOR, draft.draft_id)

    assert confirmed.confirmed is True
    assert confirmed.draft_id == draft.draft_id
    assert confirmed.total == draft.total
    assert drafts.get(VISITOR, draft.draft_id) == confirmed


def test_an_unconfirmed_draft_cannot_be_claimed(drafts: DraftStore) -> None:
    """The launch gate, checked where the write happens rather than in a prompt."""
    draft = drafts.propose(VISITOR, [burrito()])

    with pytest.raises(DraftRejectedError) as error:
        drafts.claim(VISITOR, draft.draft_id)

    assert rejection(error) is RejectionCode.DRAFT_NOT_CONFIRMED
    assert drafts.get(VISITOR, draft.draft_id) is not None


def test_a_confirmed_draft_is_claimed_once(drafts: DraftStore) -> None:
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)

    claimed = drafts.claim(VISITOR, draft.draft_id)

    assert claimed.confirmed is True
    with pytest.raises(DraftRejectedError) as error:
        drafts.claim(VISITOR, draft.draft_id)
    assert rejection(error) is RejectionCode.DRAFT_NOT_FOUND


def test_only_one_of_many_simultaneous_claims_wins(drafts: DraftStore) -> None:
    """One draft is at most one order, however many requests arrive together."""
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)
    callers = 24
    barrier = threading.Barrier(callers)

    def claim(_: int) -> bool:
        barrier.wait()
        try:
            drafts.claim(VISITOR, draft.draft_id)
        except DraftRejectedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=callers) as pool:
        won = list(pool.map(claim, range(callers)))

    assert won.count(True) == 1


# --- a draft belongs to one visitor -----------------------------------------


def test_a_stranger_cannot_confirm_someone_elses_draft(drafts: DraftStore) -> None:
    draft = drafts.propose(VISITOR, [burrito()])

    with pytest.raises(DraftRejectedError) as error:
        drafts.confirm(STRANGER, draft.draft_id)

    assert rejection(error) is RejectionCode.DRAFT_NOT_FOUND
    assert drafts.get(VISITOR, draft.draft_id).confirmed is False  # type: ignore[union-attr]


def test_a_stranger_cannot_claim_a_draft_another_visitor_confirmed(
    drafts: DraftStore,
) -> None:
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)

    with pytest.raises(DraftRejectedError) as error:
        drafts.claim(STRANGER, draft.draft_id)

    assert rejection(error) is RejectionCode.DRAFT_NOT_FOUND
    assert drafts.claim(VISITOR, draft.draft_id).draft_id == draft.draft_id


def test_a_stranger_reading_a_draft_is_told_nothing(drafts: DraftStore) -> None:
    """Someone else's well-formed id answers exactly as an invented one does."""
    draft = drafts.propose(VISITOR, [burrito()])

    assert drafts.get(STRANGER, draft.draft_id) is None
    assert drafts.get(STRANGER, "draft-invented") is None


def test_a_stranger_cannot_discard_a_draft(drafts: DraftStore) -> None:
    draft = drafts.propose(VISITOR, [burrito()])

    assert drafts.discard(STRANGER, draft.draft_id) is False
    assert drafts.get(VISITOR, draft.draft_id) is not None


def test_a_draft_needs_a_visitor(drafts: DraftStore) -> None:
    with pytest.raises(ValueError, match="belong to a visitor"):
        drafts.propose("", [burrito()])


# --- drafts expire ----------------------------------------------------------


def test_an_expired_draft_cannot_be_confirmed(
    drafts: DraftStore, clock: FakeClock
) -> None:
    draft = drafts.propose(VISITOR, [burrito()])

    clock.advance(901.0)

    with pytest.raises(DraftRejectedError) as error:
        drafts.confirm(VISITOR, draft.draft_id)
    assert rejection(error) is RejectionCode.DRAFT_EXPIRED


def test_an_expired_draft_cannot_be_claimed_even_once_confirmed(
    drafts: DraftStore, clock: FakeClock
) -> None:
    """A card left open overnight is a stale quote, confirmed or not."""
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)

    clock.advance(901.0)

    with pytest.raises(DraftRejectedError) as error:
        drafts.claim(VISITOR, draft.draft_id)
    assert rejection(error) is RejectionCode.DRAFT_EXPIRED


def test_a_draft_lives_for_its_whole_ttl(drafts: DraftStore, clock: FakeClock) -> None:
    draft = drafts.propose(VISITOR, [burrito()])

    clock.advance(899.0)

    assert drafts.confirm(VISITOR, draft.draft_id).confirmed is True


def test_the_ttl_is_configurable(catalog: MenuCatalog, clock: FakeClock) -> None:
    store = DraftStore(catalog, clock=clock, ttl_seconds=60.0)
    draft = store.propose(VISITOR, [burrito()])

    clock.advance(61.0)

    with pytest.raises(DraftRejectedError) as error:
        store.confirm(VISITOR, draft.draft_id)
    assert rejection(error) is RejectionCode.DRAFT_EXPIRED


def test_expired_drafts_are_swept_rather_than_accumulated(
    drafts: DraftStore, clock: FakeClock
) -> None:
    drafts.propose(VISITOR, [burrito()])
    clock.advance(901.0)

    drafts.propose(VISITOR, [burrito()])

    assert len(drafts) == 1


def test_the_store_is_bounded(catalog: MenuCatalog, clock: FakeClock) -> None:
    store = DraftStore(catalog, clock=clock, max_drafts=4)

    minted = [store.propose(VISITOR, [burrito()]) for _ in range(8)]

    assert len(store) <= 4
    assert store.get(VISITOR, minted[-1].draft_id) is not None
    assert store.get(VISITOR, minted[0].draft_id) is None


# --- editing in place -------------------------------------------------------


def test_editing_a_draft_produces_a_new_priced_draft(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    first = drafts.propose(VISITOR, [burrito()])

    edited = drafts.revise(
        VISITOR,
        first.draft_id,
        [
            burrito(
                selections=[
                    {"modifier_item_id": WHITE_RICE},
                    {"modifier_item_id": BLACK_BEANS},
                    {"modifier_item_id": EXTRA_CHICKEN},
                ]
            )
        ],
    )

    assert edited.draft_id != first.draft_id
    assert edited.supersedes == first.draft_id
    assert edited.total == first.total + price_of(catalog, EXTRA_CHICKEN)


def test_the_draft_an_edit_replaced_is_no_longer_confirmable(
    drafts: DraftStore,
) -> None:
    """Otherwise the old card, at the old price, is still sitting in a tab."""
    first = drafts.propose(VISITOR, [burrito()])

    drafts.revise(VISITOR, first.draft_id, [{"item_id": CHIPS}])

    with pytest.raises(DraftRejectedError) as error:
        drafts.confirm(VISITOR, first.draft_id)
    assert rejection(error) is RejectionCode.DRAFT_NOT_FOUND


def test_editing_a_confirmed_draft_starts_it_unconfirmed(drafts: DraftStore) -> None:
    """A confirmation is for a basket, and this is a different basket."""
    first = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, first.draft_id)

    edited = drafts.revise(VISITOR, first.draft_id, [{"item_id": CHIPS}])

    assert edited.confirmed is False
    with pytest.raises(DraftRejectedError) as error:
        drafts.claim(VISITOR, edited.draft_id)
    assert rejection(error) is RejectionCode.DRAFT_NOT_CONFIRMED


def test_a_rejected_edit_leaves_the_original_alone(drafts: DraftStore) -> None:
    first = drafts.propose(VISITOR, [burrito()])

    with pytest.raises(DraftRejectedError):
        drafts.revise(VISITOR, first.draft_id, [{"item_id": "CMG-9999"}])

    assert drafts.confirm(VISITOR, first.draft_id).confirmed is True


def test_an_edit_keeps_the_store_and_the_order_type(drafts: DraftStore) -> None:
    first = drafts.propose(VISITOR, [burrito()], order_type=OrderType.DELIVERY)

    edited = drafts.revise(VISITOR, first.draft_id, [burrito()])

    assert edited.order_type is OrderType.DELIVERY
    assert edited.restaurant_id == first.restaurant_id


def test_a_stranger_cannot_edit_a_draft(drafts: DraftStore) -> None:
    first = drafts.propose(VISITOR, [burrito()])

    with pytest.raises(DraftRejectedError) as error:
        drafts.revise(STRANGER, first.draft_id, [{"item_id": CHIPS}])

    assert rejection(error) is RejectionCode.DRAFT_NOT_FOUND
    assert drafts.get(VISITOR, first.draft_id) is not None


def test_an_expired_draft_cannot_be_edited(drafts: DraftStore, clock: FakeClock) -> None:
    first = drafts.propose(VISITOR, [burrito()])

    clock.advance(901.0)

    with pytest.raises(DraftRejectedError) as error:
        drafts.revise(VISITOR, first.draft_id, [{"item_id": CHIPS}])
    assert rejection(error) is RejectionCode.DRAFT_EXPIRED


# --- the card ---------------------------------------------------------------


def test_the_card_is_json_and_money_is_never_a_float(
    drafts: DraftStore, catalog: MenuCatalog
) -> None:
    draft = drafts.propose(VISITOR, [burrito(), {"item_id": CHIPS, "quantity": 2}])

    card = json.loads(json.dumps(drafts.card(draft)))

    assert card["draft_id"] == draft.draft_id
    assert Decimal(card["total"]) == draft.total
    assert card["order_type"] == "pickup"
    assert card["requires_confirmation"] is True
    assert card["confirmed"] is False
    assert [line["item_id"] for line in card["lines"]] == [BURRITO, CHIPS]
    assert Decimal(card["lines"][1]["line_total"]) == price_of(catalog, CHIPS) * 2
    assert card["pricing"]["restaurant_id"] == RESTAURANT
    assert card["pricing"]["content_version"] == catalog.content_version()


def test_the_card_names_the_store_it_is_priced_at(drafts: DraftStore) -> None:
    draft = drafts.propose(VISITOR, [burrito()])

    store = drafts.card(draft)["store"]

    assert store["restaurant_id"] == RESTAURANT
    assert store["name"] == "Lakewood Mall"
    assert store["address"] == "5310 Lakewood Blvd, Lakewood, CA 90712"


def test_the_card_says_the_order_is_simulated(drafts: DraftStore) -> None:
    """PRD T5, on every card, without exception."""
    draft = drafts.propose(VISITOR, [burrito()])

    assert "Simulated order" in str(drafts.card(draft)["notice"])
