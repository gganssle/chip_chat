"""Splitting a published policy page into sections.

The property under test is issue #21's: the boundaries the page publishes have
to survive the harvest, because a document flattened to one string cannot be
chunked by section later. Everything here runs on markup, never on a network.
"""

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.sources.chipotle import ChipotleSourceError, parse_document


def read(name: str) -> str:
    """Return one fixture page's source."""
    return site.read(name).decode()


def test_a_bold_only_paragraph_starts_a_section() -> None:
    document = parse_document(read("rewards-terms.html"), "terms")

    headings = [section.heading for section in document.sections]
    assert "ELIGIBILITY" in headings
    assert "ACCUMULATING POINTS" in headings


def test_a_bold_lead_in_inside_a_paragraph_does_not() -> None:
    """``<b>Mandatory ...: </b>Should a Dispute arise`` is prose, not a boundary."""
    document = parse_document(read("rewards-terms.html"), "terms")

    headings = [section.heading for section in document.sections]
    assert not any(
        heading is not None and heading.startswith("Mandatory") for heading in headings
    )
    arbitration = next(
        section
        for section in document.sections
        if section.heading == "ARBITRATION AGREEMENT"
    )
    assert "Mandatory Pre-Arbitration" in arbitration.text


def test_the_sections_of_one_heading_stay_with_it() -> None:
    document = parse_document(read("rewards-terms.html"), "terms")

    eligibility = next(
        section for section in document.sections if section.heading == "ELIGIBILITY"
    )
    assert "legal residents of the 50 United States" in eligibility.text
    assert "Commercial use is prohibited." in eligibility.text
    assert "accumulate points" not in eligibility.text


def test_a_bullet_in_front_of_a_heading_does_not_stop_it_being_one() -> None:
    """The terms decorate one section with ``&middot;&nbsp;&nbsp;`` outside the bold."""
    document = parse_document(read("rewards-terms.html"), "terms")

    waiver = next(
        section
        for section in document.sections
        if section.heading is not None and "CLASS ACTION WAIVER" in section.heading
    )
    assert waiver.text.startswith("YOU AND WE EACH AGREE")
    arbitration = next(
        section
        for section in document.sections
        if section.heading == "ARBITRATION AGREEMENT"
    )
    assert "INDIVIDUAL BASIS" not in arbitration.text


def test_the_prose_before_the_first_heading_is_its_own_section() -> None:
    document = parse_document(read("rewards-terms.html"), "terms")

    assert document.sections[0].heading is None
    assert document.sections[0].text == "(LAST UPDATED APRIL 13, 2026)"


def test_the_page_title_is_read_from_its_own_title_component() -> None:
    document = parse_document(read("rewards-terms.html"), "terms")

    assert document.title == "CHIPOTLE REWARDS - TERMS AND CONDITIONS"


def test_text_outside_main_is_not_part_of_the_document() -> None:
    """The header and footer carry ``cmp-text`` blocks too. They are navigation."""
    document = parse_document(read("rewards-terms.html"), "terms")

    assert not any("MENU CATERING" in section.text for section in document.sections)
    assert not any(
        "Chipotle Mexican Grill" in section.text for section in document.sections
    )


def test_a_block_the_page_renders_twice_lands_once() -> None:
    """The rewards page ships one block per breakpoint. Both say the same thing."""
    document = parse_document(read("rewards.html"), "rewards")

    adding_up = [
        section for section in document.sections if "KEEPS ADDING UP" in section.text
    ]
    assert len(adding_up) == 1


def test_a_page_with_no_authored_text_raises() -> None:
    with pytest.raises(ChipotleSourceError, match="published no text inside <main>"):
        parse_document("<html><body><p>nothing authored</p></body></html>", "empty")
