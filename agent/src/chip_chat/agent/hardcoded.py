"""Three menu items, one account, one store. All of it invented, none of it real.

This module is the "hardcoded" in issue #16's *hardcoded data*, and the literal
dict below is the correct implementation rather than a shortcut apologised for
in a comment. The slice exists to force the deployment and authentication story
out into the open while the scope is still small enough to debug; a real menu
behind it would mean debugging the harvest at the same time, which is the thing
the issue is written to prevent.

**Everything here is deleted, not extended.** The real versions arrive as
``menu_catalog`` from the harvest (issue #22), the synthetic accounts from the
generator (#29) and the gold marts from Databricks (#38). Nothing downstream
should grow a dependency on these names -- :mod:`chip_chat.agent.tools` is the
only importer, and that is the seam the real sources land on.

The shapes, though, are not invented. An order draft carries the fields
``docs/action-surface.md`` §7.1 pins down -- a store, an order type, lines with
selections -- because the *shape* is what the agent, the widget and the span
schema are all written against, and a shape that changes when the data becomes
real would make this week's work worth nothing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

__all__ = [
    "ACCOUNT",
    "MENU",
    "SIMULATION_NOTICE",
    "STORE",
    "Account",
    "MenuItem",
    "Store",
    "menu_item",
    "search_menu",
]

SIMULATION_NOTICE = (
    "Simulated order. Nothing was cooked, charged or sent to a restaurant."
)
"""PRD T5: every action is simulated and every card has to say so."""


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One orderable item, priced at one store."""

    item_id: str
    name: str
    description: str
    unit_price: Decimal
    calories: int
    allergens: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    """Extra words a visitor might use that are not in the name or description."""

    def summary(self) -> str:
        """One line of prose, which is what the retrieval lane returns today."""
        allergens = ", ".join(self.allergens) if self.allergens else "none declared"
        return (
            f"{self.name} (${self.unit_price}). {self.description} "
            f"{self.calories} calories. Allergens: {allergens}."
        )


@dataclass(frozen=True, slots=True)
class Store:
    """The one store this slice can order from."""

    restaurant_id: str
    name: str
    address: str


@dataclass(frozen=True, slots=True)
class Account:
    """The one loaded persona every visitor is assigned on entry.

    Loaded on purpose. The public-demo phase turns on one observation -- a
    visitor with an empty account has nothing to ask -- and a slice whose
    account question answers "you have 0 points" would not exercise the lane it
    exists to exercise.
    """

    persona_id: str
    display_name: str
    points_balance: int
    member_since: str
    home_store: Store
    usual_order: str
    favourite_items: tuple[str, ...] = field(default_factory=tuple)


STORE = Store(
    restaurant_id="store-0001",
    name="Ballard",
    address="1234 NW Market St, Seattle, WA",
)

MENU: Mapping[str, MenuItem] = {
    item.item_id: item
    for item in (
        MenuItem(
            item_id="BOWL-CHICKEN",
            name="Chicken Burrito Bowl",
            description=(
                "Grilled chicken over cilantro-lime white rice with black beans, "
                "fresh tomato salsa, cheese and lettuce."
            ),
            unit_price=Decimal("10.70"),
            calories=790,
            allergens=("milk",),
            keywords=("bowl", "chicken", "burrito bowl"),
        ),
        MenuItem(
            item_id="BURRITO-BARBACOA",
            name="Barbacoa Burrito",
            description=(
                "Barbacoa -- beef braised with chipotle adobo, cumin, cloves and "
                "bay -- wrapped in a flour tortilla with rice, beans and salsa. "
                "Warmly spiced rather than hot."
            ),
            unit_price=Decimal("11.95"),
            calories=1075,
            allergens=("wheat", "milk"),
            keywords=("burrito", "barbacoa", "beef", "spicy"),
        ),
        MenuItem(
            item_id="SIDE-GUACAMOLE",
            name="Side of Guacamole",
            description=(
                "Hass avocado mashed with lime, cilantro, red onion, jalapeno and "
                "salt. Six ingredients, no dairy, no gluten."
            ),
            unit_price=Decimal("2.90"),
            calories=230,
            keywords=("guac", "guacamole", "avocado", "vegan"),
        ),
    )
}
"""The whole catalogue. Three rows, keyed by ``item_id``."""

ACCOUNT = Account(
    persona_id="persona-loyal-regular",
    display_name="the Ballard regular",
    points_balance=1_340,
    member_since="2024-03-11",
    home_store=STORE,
    usual_order="a chicken burrito bowl with a side of guac",
    favourite_items=("BOWL-CHICKEN", "SIDE-GUACAMOLE"),
)


def menu_item(item_id: str) -> MenuItem | None:
    """Return the catalogue row for ``item_id``, or ``None`` if there is none."""
    return MENU.get(item_id)


def search_menu(query: str, *, limit: int = 3) -> Sequence[tuple[MenuItem, float]]:
    """Score the three items against ``query`` and return the best of them.

    Word overlap, and deliberately nothing cleverer. The real lane is hybrid
    retrieval over an AI Search index (issue #45); pretending to be that here
    would produce a scoring function somebody later mistook for a baseline.

    Args:
        query: What the visitor asked.
        limit: How many items to return at most.

    Returns:
        ``(item, score)`` pairs, best first, omitting items that match nothing.
        Empty when the query is about something not on this three-item menu,
        which is a real answer and not a failure.
    """
    words = {word for word in _normalise(query).split() if len(word) > 2}
    if not words:
        return ()
    scored: list[tuple[MenuItem, float]] = []
    for item in MENU.values():
        haystack = _normalise(
            " ".join((item.name, item.description, *item.keywords))
        ).split()
        hits = sum(1 for word in words if any(word in token for token in haystack))
        if hits:
            scored.append((item, round(hits / len(words), 3)))
    scored.sort(key=lambda pair: (-pair[1], pair[0].item_id))
    return tuple(scored[:limit])


def _normalise(text: str) -> str:
    """Lowercase and strip punctuation, so "guac?" matches "guac"."""
    return "".join(
        character if character.isalnum() else " " for character in text.lower()
    )
