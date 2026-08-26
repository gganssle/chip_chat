"""The three-item menu, and the search that stands in for retrieval."""

from decimal import Decimal

from chip_chat.agent.hardcoded import ACCOUNT, MENU, STORE, menu_item, search_menu


def test_the_menu_is_three_items() -> None:
    """The number is load-bearing: the system prompt enumerates them all."""
    assert len(MENU) == 3
    assert set(MENU) == {"BOWL-CHICKEN", "BURRITO-BARBACOA", "SIDE-GUACAMOLE"}


def test_every_item_is_keyed_by_its_own_id() -> None:
    for item_id, item in MENU.items():
        assert item.item_id == item_id


def test_prices_are_decimals() -> None:
    """Floats would make a total that is a cent out, which a card would show."""
    for item in MENU.values():
        assert isinstance(item.unit_price, Decimal)


def test_summary_names_allergens_even_when_there_are_none() -> None:
    """ "Allergens: none declared" is an answer; a missing line is not."""
    assert "none declared" in MENU["SIDE-GUACAMOLE"].summary()
    assert "milk" in MENU["BOWL-CHICKEN"].summary()


def test_search_finds_an_item_by_a_word_from_its_description() -> None:
    hits = search_menu("is the barbacoa spicy?")
    assert hits
    assert hits[0][0].item_id == "BURRITO-BARBACOA"


def test_search_finds_an_item_by_a_word_nobody_put_in_the_name() -> None:
    """ "guac" is what a visitor types; "Guacamole" is what the menu says."""
    hits = search_menu("can I get guac")
    assert [item.item_id for item, _ in hits] == ["SIDE-GUACAMOLE"]


def test_search_returns_nothing_for_something_not_on_the_menu() -> None:
    """A real answer. The tool turns it into "the menu is three items"."""
    assert search_menu("do you have sushi") == ()


def test_search_ignores_a_query_of_only_short_words() -> None:
    assert search_menu("is it?") == ()


def test_scores_are_between_zero_and_one() -> None:
    for _, score in search_menu("chicken bowl with guacamole"):
        assert 0 < score <= 1


def test_menu_item_returns_none_for_an_unknown_id() -> None:
    assert menu_item("BOWL-TOFU") is None
    assert menu_item("BOWL-CHICKEN") is not None


def test_the_account_is_loaded() -> None:
    """A visitor with an empty account has nothing to ask -- see web/copy.py."""
    assert ACCOUNT.points_balance > 0
    assert ACCOUNT.usual_order
    assert ACCOUNT.home_store is STORE
