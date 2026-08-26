"""That the rewards programme is read from the harvest and never guessed.

The value of :mod:`chip_chat.data_gen.rewards` is entirely in what it refuses
to do. It will not supply an earn rate that is no longer published, it will not
pick between two pages that disagree, and it will not redeem against an empty
Rewards Exchange. Most of what follows is therefore a test that it raises.

Every input here is the harvest's own fixture site, written to a blob store the
way the real pipeline writes it and read back the way the real pipeline reads
it, so a policy table that changed shape fails here.
"""

import json

import pytest
from population_fixtures import fixture_policy, fixture_terms

from chip_chat.data_gen import RewardsTermsError
from chip_chat.data_gen.rewards import (
    DAILY_PURCHASES,
    EXPIRY_DAYS,
    POINTS_PER_DOLLAR,
    load_rewards_terms,
    rewards_by_name,
)
from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.sources.chipotle import DEFAULT_POLICY_PREFIX

POLICY = DEFAULT_POLICY_PREFIX.strip("/")


@pytest.fixture
def blobs() -> InMemoryBlobStore:
    """A store with the parsed policy harvest written into it."""
    store = InMemoryBlobStore()
    fixture_policy().write(store)
    return store


def rewrite(blobs: InMemoryBlobStore, table: str, rows: list[dict]) -> None:
    """Replace one policy table with rows of one's own."""
    body = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )
    blobs.write(f"{POLICY}/{table}.jsonl", body.encode("utf-8"))


def rows(blobs: InMemoryBlobStore, table: str) -> list[dict]:
    """Read one policy table back as plain mappings."""
    body = (blobs.read(f"{POLICY}/{table}.jsonl") or b"").decode("utf-8")
    return [json.loads(line) for line in body.splitlines()]


# --------------------------------------------------------------------------
# What it reads
# --------------------------------------------------------------------------


def test_it_reads_the_rate_chipotle_publishes(blobs: InMemoryBlobStore) -> None:
    """Ten points per dollar — the FAQ's number and the rewards page's."""
    assert load_rewards_terms(blobs).points_per_dollar == 10


def test_it_reads_the_published_expiry_window(blobs: InMemoryBlobStore) -> None:
    assert load_rewards_terms(blobs).inactivity_expiry_days == 365


def test_it_reads_the_published_daily_limit(blobs: InMemoryBlobStore) -> None:
    """Spelled "three" in the terms, which is why the reader knows words."""
    assert load_rewards_terms(blobs).daily_qualifying_purchases == 3


def test_the_rewards_are_the_harvested_rows_themselves(
    blobs: InMemoryBlobStore,
) -> None:
    """Issue #27's third acceptance criterion: the sets match exactly.

    Not "the same names" — the same rows, in the same published order, with the
    published point costs. There is no second copy of the Rewards Exchange in
    this package for the first one to drift from.
    """
    assert load_rewards_terms(blobs).rewards == fixture_policy().rewards


def test_every_rule_carries_the_page_it_was_read_from(
    blobs: InMemoryBlobStore,
) -> None:
    """A number in a column labelled as Chipotle's should name its source."""
    citations = load_rewards_terms(blobs).citations

    assert {citation.rule for citation in citations} == {
        POINTS_PER_DOLLAR,
        EXPIRY_DAYS,
        DAILY_PURCHASES,
    }
    assert all(citation.source_url.startswith("https://") for citation in citations)


def test_the_rate_is_corroborated_by_two_published_documents(
    blobs: InMemoryBlobStore,
) -> None:
    """Chipotle states it on the rewards page and again in the FAQ."""
    cited = [
        citation
        for citation in load_rewards_terms(blobs).citations
        if citation.rule == POINTS_PER_DOLLAR
    ]

    assert len({citation.source_url for citation in cited}) == 2
    assert {citation.value for citation in cited} == {10}


def test_the_cheapest_and_costliest_rewards_are_the_published_ends(
    blobs: InMemoryBlobStore,
) -> None:
    terms = load_rewards_terms(blobs)

    assert terms.cheapest.point_cost == min(r.point_cost for r in terms.rewards)
    assert terms.costliest.point_cost == max(r.point_cost for r in terms.rewards)


def test_affordable_is_what_a_balance_covers(blobs: InMemoryBlobStore) -> None:
    terms = load_rewards_terms(blobs)

    assert terms.affordable(terms.cheapest.point_cost - 1) == ()
    assert terms.affordable(terms.costliest.point_cost) == terms.rewards


def test_rewards_index_by_their_published_name(blobs: InMemoryBlobStore) -> None:
    """``loyalty_ledger.reward_name`` joins back onto this, and nothing else."""
    terms = load_rewards_terms(blobs)

    assert rewards_by_name(terms.rewards).keys() == {
        reward.name for reward in terms.rewards
    }


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_a_missing_policy_harvest_is_an_error(blobs: InMemoryBlobStore) -> None:
    with pytest.raises(RewardsTermsError, match="policy harvest"):
        load_rewards_terms(InMemoryBlobStore())


def test_an_empty_rewards_exchange_is_an_error(blobs: InMemoryBlobStore) -> None:
    """A ledger with nothing to redeem against would redeem against a guess."""
    rewrite(blobs, "rewards", [])

    with pytest.raises(RewardsTermsError, match="no rewards"):
        load_rewards_terms(blobs)


def test_an_unpublished_earn_rate_is_an_error(blobs: InMemoryBlobStore) -> None:
    """There is no default. That absence is the whole design."""
    kept = [row for row in rows(blobs, "policy_sections") if "points" not in row["text"]]
    rewrite(blobs, "policy_sections", kept)
    rewrite(blobs, "faq_entries", [])

    with pytest.raises(RewardsTermsError, match=POINTS_PER_DOLLAR):
        load_rewards_terms(blobs)


def test_two_pages_that_disagree_about_the_rate_are_an_error(
    blobs: InMemoryBlobStore,
) -> None:
    """A harvest caught mid-change is not a harvest to pick a number out of."""
    edited = rows(blobs, "faq_entries")
    for row in edited:
        row["answer"] = row["answer"].replace("10 points per $1", "12 points per $1")
    rewrite(blobs, "faq_entries", edited)

    with pytest.raises(RewardsTermsError, match="disagree"):
        load_rewards_terms(blobs)


def test_a_daily_limit_spelled_in_a_word_nobody_knows_is_an_error(
    blobs: InMemoryBlobStore,
) -> None:
    """Better a person reads the changed terms than a number is invented."""
    edited = rows(blobs, "policy_sections")
    for row in edited:
        row["text"] = row["text"].replace(
            "three qualifying purchases", "umpteen qualifying purchases"
        )
    rewrite(blobs, "policy_sections", edited)

    with pytest.raises(RewardsTermsError, match=DAILY_PURCHASES):
        load_rewards_terms(blobs)


def test_a_column_that_changed_type_is_an_error(blobs: InMemoryBlobStore) -> None:
    """Coercing it would put a guess where a published point cost belongs."""
    edited = rows(blobs, "rewards")
    edited[0]["point_cost"] = "85"
    rewrite(blobs, "rewards", edited)

    with pytest.raises(RewardsTermsError, match="not an integer"):
        load_rewards_terms(blobs)


def test_a_column_that_went_missing_is_an_error(blobs: InMemoryBlobStore) -> None:
    edited = rows(blobs, "rewards")
    del edited[0]["point_cost"]
    rewrite(blobs, "rewards", edited)

    with pytest.raises(RewardsTermsError, match="missing point_cost"):
        load_rewards_terms(blobs)


def test_a_column_that_appeared_is_an_error(blobs: InMemoryBlobStore) -> None:
    """A parser that grew a column is a parser this reader has not been read against."""
    edited = rows(blobs, "rewards")
    edited[0]["redeemable_item_id"] = "CMG-1234"
    rewrite(blobs, "rewards", edited)

    with pytest.raises(RewardsTermsError, match="no column redeemable_item_id"):
        load_rewards_terms(blobs)


# --------------------------------------------------------------------------
# The digest
# --------------------------------------------------------------------------


def test_the_content_version_ignores_when_the_harvest_ran() -> None:
    """Two readings of an unchanged programme compute the same ledger."""
    first, second = InMemoryBlobStore(), InMemoryBlobStore()
    fixture_policy().write(first)
    fixture_policy().write(second, prefix=DEFAULT_POLICY_PREFIX)

    assert (
        load_rewards_terms(first).content_version()
        == load_rewards_terms(second).content_version()
    )


def test_the_content_version_moves_when_a_published_price_does(
    blobs: InMemoryBlobStore,
) -> None:
    before = load_rewards_terms(blobs).content_version()
    edited = rows(blobs, "rewards")
    edited[0]["point_cost"] += 5
    rewrite(blobs, "rewards", edited)

    assert load_rewards_terms(blobs).content_version() != before


def test_the_fixture_terms_are_the_ones_the_ledger_tests_use() -> None:
    """So that a change here is a change the ledger tests see too."""
    assert fixture_terms().rewards == fixture_policy().rewards
