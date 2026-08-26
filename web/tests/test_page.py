"""The page, and the three things on it that are requirements rather than style."""

from chip_chat.agent.hardcoded import ACCOUNT
from chip_chat.api.outcome import STOP_STATE_MESSAGE
from chip_chat.web import chat_page, stop_page
from chip_chat.web.copy import BANNER, SUGGESTIONS


def test_the_disclosure_is_above_the_fold_on_both_pages() -> None:
    """It is a disclosure, so it is not allowed to live only in a footer."""
    for page in (chat_page(), stop_page(STOP_STATE_MESSAGE)):
        assert BANNER in page
        assert page.index(BANNER) < page.index("<main")


def test_both_pages_ask_not_to_be_indexed() -> None:
    for page in (chat_page(), stop_page(STOP_STATE_MESSAGE)):
        assert 'name="robots" content="noindex,nofollow"' in page


def test_the_opening_message_quotes_the_real_balance() -> None:
    """One source for the number, so the copy and the tool cannot disagree."""
    assert f"{ACCOUNT.points_balance:,} points" in chat_page()


def test_the_suggestions_are_the_three_interactions() -> None:
    page = chat_page()
    assert len(SUGGESTIONS) == 3
    for suggestion in SUGGESTIONS:
        assert suggestion in page


def test_the_page_is_self_contained() -> None:
    """No build step and no third-party origin: the CSP surface is the page."""
    page = chat_page()
    assert "<script src=" not in page
    assert 'rel="stylesheet"' not in page
    assert "http://" not in page


def test_the_confirm_button_sends_the_draft_id_to_the_server() -> None:
    """Pressing it is what confirms an order, and the press has to leave the browser."""
    page = chat_page()
    assert "confirm_draft_id" in page
    assert "Confirm order" in page


def test_the_stop_state_never_reads_as_a_failure() -> None:
    """PRD S4. Nothing failed -- the cap worked."""
    page = stop_page(STOP_STATE_MESSAGE)
    assert STOP_STATE_MESSAGE in page
    for word in ("error", "quota", "sorry", "failed"):
        assert word not in page.lower()


def test_the_stop_state_has_no_composer() -> None:
    """Offering a text box that cannot be answered would be a worse experience."""
    assert "<form" not in stop_page(STOP_STATE_MESSAGE)
