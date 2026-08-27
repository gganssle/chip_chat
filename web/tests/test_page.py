"""The page, and the things on it that are requirements rather than style.

Every test here is an acceptance criterion from #67, #68 or #70 rather than a
rendering preference. The page is one string, so they are assertions about a
string; that is the trade the no-build-step decision makes, and it is a good one
while the requirements are *"the disclosure is above the fold"* and *"the word
simulated is on the card"* rather than *"the card is 8px from the edge"*.
"""

import pytest

from chip_chat.api.outcome import STOP_STATE_MESSAGE
from chip_chat.web import chat_page, stop_page
from chip_chat.web.copy import BANNER, SIMULATED, SWITCH_LABEL
from chip_chat.web.persona import (
    Persona,
    opening_message,
    restart_message,
    suggestions,
    unbound_opening_message,
)

REGULAR = Persona(
    persona_id="regular",
    label="The Weekly Regular",
    display_name="Sam",
    narrative=(
        "a regular at NC Town 1 Mall, 1,288 points on the card, and 99% of 79 "
        "orders the same Chicken Bowl with guacamole, white rice, black beans "
        "and cheese."
    ),
    home_store_name="NC Town 1 Mall",
    points_balance=1_288,
    order_count=79,
)

LAPSED = Persona(
    persona_id="lapsed",
    label="The Lapsed Regular",
    display_name="Alex",
    narrative=(
        "a regular at CO Town 1 Mall until March 2026, and not seen since -- "
        "1,904 points still unredeemed from 51 orders."
    ),
    home_store_name="CO Town 1 Mall",
    points_balance=1_904,
    order_count=51,
)

MANAGER = Persona(
    persona_id="office_manager",
    label="The Office Manager",
    display_name="Robin",
    narrative=(
        "puts in the floor's lunch order at TX Town 1 Mall: 61 orders averaging "
        "4.2 entrees, $3,102 in all."
    ),
    home_store_name="TX Town 1 Mall",
    points_balance=980,
    order_count=61,
)

EVERY_PERSONA = (REGULAR, LAPSED, MANAGER)


# ---------------------------------------------------------------------------
# #70 -- the framing, which is a launch criterion
# ---------------------------------------------------------------------------


def test_the_disclosure_is_above_the_fold_on_both_pages() -> None:
    """It is a disclosure, so it is not allowed to live only in a footer."""
    for page in (chat_page(), stop_page(STOP_STATE_MESSAGE)):
        assert BANNER in page
        assert page.index(BANNER) < page.index("<main")


def test_the_disclosure_says_exactly_what_the_launch_criterion_asks_for() -> None:
    """Issue #70 quotes the sentence. This is that sentence."""
    assert BANNER == (
        "Unofficial demo, not affiliated with Chipotle Mexican Grill. All orders "
        "are simulated."
    )


def test_the_disclosure_persists_rather_than_scrolling_away() -> None:
    """*Not a dismissible toast* -- and not a block at the top of the document.

    The banner is sticky, so it is on screen in the conversation as well as on
    the entry screen, which is the second half of #70's first criterion.
    """
    page = chat_page()
    assert ".banner { position:sticky" in page
    for dismissal in ("dismiss", 'aria-label="Close', "banner-close"):
        assert dismissal not in page


def test_nothing_on_the_page_borrows_the_incumbents_branding() -> None:
    """No logo, no wordmark, no brand colour. ``docs/public-demo.md`` §4."""
    page = chat_page().lower()
    for borrowed in ("chipotle.com", "logo", "wordmark", "svg", "<img src"):
        assert borrowed not in page
    for brand_colour in ("#a81612", "#451400", "#ac2318"):
        assert brand_colour not in page


def test_both_pages_ask_not_to_be_indexed() -> None:
    assert 'name="robots" content="noindex,nofollow"' in chat_page()
    assert 'name="robots" content="noindex,nofollow"' in stop_page(STOP_STATE_MESSAGE)


def test_the_page_is_self_contained() -> None:
    """No build step and no third-party origin: the CSP surface is the page."""
    page = chat_page()
    assert "<script src=" not in page
    assert 'rel="stylesheet"' not in page
    assert "http://" not in page


# ---------------------------------------------------------------------------
# #67 -- the opening message and the chips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("persona", EVERY_PERSONA, ids=lambda item: item.persona_id)
def test_the_opening_message_quotes_the_fixtures_narrative(persona: Persona) -> None:
    """Store, points and characteristic order, in the sentence a person wrote."""
    message = opening_message(persona)

    assert (persona.narrative or "").rstrip(".") in message
    assert (persona.display_name or "") in message
    assert message.count("..") == 0


@pytest.mark.parametrize("persona", EVERY_PERSONA, ids=lambda item: item.persona_id)
def test_the_opening_message_is_grammatical_for_every_archetype(
    persona: Persona,
) -> None:
    """The narratives are noun phrases and verb phrases, so one frame will not do.

    *"I've set you up as puts in the floor's lunch order"* is the failure this
    is written against: a lead-in that fits five archetypes and mangles two.
    """
    message = opening_message(persona)

    assert "You're puts" not in message
    assert "You're turns" not in message
    assert "You're feeds" not in message
    assert message.startswith(f"Hi {persona.display_name}.")


def test_a_visitor_who_gave_no_name_still_gets_a_greeting() -> None:
    """The gate is a greeting, not a login: the name is optional throughout."""
    message = opening_message(
        Persona("regular", "The Weekly Regular", None, "a regular at X")
    )

    assert message.startswith("Hi there.")


def test_the_invitation_is_specific_to_the_archetype() -> None:
    """A generic "ask me anything" is the blank prompt PRD §06 says loses them."""
    assert "points are still sitting there" in opening_message(LAPSED)
    assert "for eleven people" in opening_message(MANAGER)


@pytest.mark.parametrize("persona", EVERY_PERSONA, ids=lambda item: item.persona_id)
def test_the_chips_span_at_least_three_lanes(persona: Persona) -> None:
    """#67: *tappable chips, spanning at least three different capabilities*."""
    chips = suggestions(persona)

    assert len({chip.lane for chip in chips}) >= 3
    assert {"menu", "account", "order"} <= {chip.lane for chip in chips}


def test_the_chips_are_worded_for_the_persona_holding_them() -> None:
    """*Genuinely answerable by the assigned persona* -- so they differ."""
    regular = {chip.prompt for chip in suggestions(REGULAR)}
    lapsed = {chip.prompt for chip in suggestions(LAPSED)}

    assert regular != lapsed
    assert "What's my usual?" in regular
    assert "What are my points worth?" in lapsed


def test_a_deployment_with_no_population_says_so_rather_than_inventing_one() -> None:
    """An invented account is the empty account #66 refuses. Say it instead."""
    message = unbound_opening_message()

    assert "no synthetic accounts loaded" in message
    assert "points" in message


# ---------------------------------------------------------------------------
# #69 -- the switch says the conversation restarted
# ---------------------------------------------------------------------------


def test_the_restart_message_says_the_conversation_restarted() -> None:
    """#69's second criterion, in the only place a visitor can read it."""
    message = restart_message(LAPSED)

    assert message.startswith("Starting over.")
    assert "belonged to somebody else" in message
    assert (LAPSED.narrative or "").rstrip(".") in message


def test_the_switcher_is_on_the_chat_surface() -> None:
    """One tap, from the conversation -- *not buried in a settings screen*."""
    page = chat_page()

    assert SWITCH_LABEL in page
    assert page.index('id="switch"') < page.index('id="log"')


# ---------------------------------------------------------------------------
# #68 -- the card, the receipt, and the word on both
# ---------------------------------------------------------------------------


def test_the_card_carries_the_layout_the_prd_specifies() -> None:
    """PRD Flow 3: a title, the modifiers, a rule, the money, then the actions."""
    page = chat_page()

    for piece in ("lineTitle", "modifierText", "renderCard", "pts available"):
        assert piece in page
    assert "'Place order'" in page
    assert "'Edit'" in page


def test_simulated_is_on_the_card_and_on_the_receipt() -> None:
    """#68: *not in a footnote -- on the card*, and on every receipt too."""
    page = chat_page()

    # Once in the confirmation card's action row, once in the editor's, and
    # once more on a receipt, which reuses the card renderer.
    assert page.count("'\\u00b7 ' + SIMULATED") == 2
    assert f'const SIMULATED = "{SIMULATED}"' in page


def test_a_receipt_says_it_can_be_referred_back_to() -> None:
    """*Receipts persist in the conversation and can be referenced later.*"""
    assert "Kept in this conversation" in chat_page()


def test_editing_a_card_re_prices_it_rather_than_placing_it() -> None:
    """Editing produces a new priced draft; it never confirms one."""
    page = chat_page()

    assert "/api/draft/revise" in page
    assert "'Re-price'" in page


def test_the_confirm_button_sends_the_draft_id_to_the_server() -> None:
    """Pressing it is what confirms an order, and the press has to leave the browser."""
    page = chat_page()

    assert "confirm_draft_id" in page
    assert "Place order" in page


def test_a_photograph_appears_in_the_transcript() -> None:
    """*The photo visible in the transcript next to what Cilantro thought it saw.*"""
    page = chat_page()

    assert "/api/photo" in page
    assert "createObjectURL" in page
    assert 'accept="image/*"' in page


def test_the_widget_reads_the_turn_as_a_stream() -> None:
    """A two-second turn should paint rather than hang."""
    page = chat_page()

    assert "application/x-ndjson" in page
    assert "getReader()" in page


# ---------------------------------------------------------------------------
# The phone, and the stop state
# ---------------------------------------------------------------------------


def test_the_page_is_built_for_a_phone_first() -> None:
    """*Most people receiving this link will open it on one.*"""
    page = chat_page()

    assert "width=device-width" in page
    assert "viewport-fit=cover" in page
    assert "safe-area-inset-bottom" in page
    assert "@media (max-width: 26rem)" in page


def test_the_stop_state_never_reads_as_a_failure() -> None:
    """PRD S4. Nothing failed -- the cap worked."""
    page = stop_page(STOP_STATE_MESSAGE)
    assert STOP_STATE_MESSAGE in page
    for word in ("error", "quota", "sorry", "failed"):
        assert word not in page.lower()


def test_the_stop_state_has_no_composer() -> None:
    """Offering a text box that cannot be answered would be a worse experience."""
    assert "<form" not in stop_page(STOP_STATE_MESSAGE)
