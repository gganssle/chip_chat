"""The month's semantic requests, counted — and the boundary it resets on.

Both of the interesting properties here are about *time*, so the clock is
injected and no test waits for one.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chip_chat.search.allowance import (
    FREE_TIER_SEMANTIC_REQUESTS,
    FileAllowanceStore,
    InMemoryAllowanceStore,
    SemanticAllowance,
    month_of,
)


class FrozenClock:
    """A clock that says what it is told to."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


AUGUST = datetime(2026, 8, 27, 5, 4, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 1, 0, 1, tzinfo=UTC)


def test_the_free_tier_ceiling_is_a_thousand_a_month() -> None:
    # Not a price. Past it the API returns a billing error rather than a
    # charge, which is the whole reason the degrade path exists.
    assert FREE_TIER_SEMANTIC_REQUESTS == 1000


def test_requests_are_spent_until_the_ceiling_and_then_refused() -> None:
    allowance = SemanticAllowance(limit=3, clock=FrozenClock(AUGUST))
    assert [allowance.spend() for _ in range(4)] == [True, True, True, False]
    assert allowance.report().remaining == 0
    assert allowance.report().exhausted


def test_a_refused_claim_counts_nothing() -> None:
    # The caller degrades and answers anyway, so a refusal must not consume the
    # request it declined to grant.
    allowance = SemanticAllowance(limit=1, clock=FrozenClock(AUGUST))
    allowance.spend()
    allowance.spend()
    assert allowance.report().spent == 1


def test_the_count_resets_on_a_calendar_month() -> None:
    clock = FrozenClock(AUGUST)
    allowance = SemanticAllowance(limit=2, clock=clock)
    assert allowance.spend()
    assert allowance.spend()
    assert not allowance.spend()
    clock.moment = SEPTEMBER
    assert allowance.report().spent == 0
    assert allowance.spend()


def test_the_service_outranks_the_counter() -> None:
    # The counter is an estimate of a number Azure holds. When a semantic
    # request is refused while the count says there is room -- something else
    # spent the allowance -- the service wins for the rest of the month.
    allowance = SemanticAllowance(limit=1000, clock=FrozenClock(AUGUST))
    allowance.spend()
    allowance.exhaust("Semantic search quota exceeded for this billing plan.")
    assert not allowance.spend()
    report = allowance.report()
    assert report.exhausted
    assert report.reason is not None
    assert "quota" in report.reason


def test_the_month_is_read_in_utc() -> None:
    assert month_of(AUGUST) == "2026-08"
    assert month_of(SEPTEMBER) == "2026-09"


def test_a_file_backed_count_survives_the_process_that_made_it(
    tmp_path: Path,
) -> None:
    # The failure this exists to prevent: an evaluation sweep spending the
    # month's allowance twice because nothing was keeping score between runs.
    path = tmp_path / "nested" / "semantic-allowance.json"
    first = SemanticAllowance(
        limit=5, store=FileAllowanceStore(path), clock=FrozenClock(AUGUST)
    )
    for _ in range(5):
        assert first.spend()

    second = SemanticAllowance(
        limit=5, store=FileAllowanceStore(path), clock=FrozenClock(AUGUST)
    )
    assert not second.spend()
    assert second.report().spent == 5


def test_a_missing_or_unreadable_file_is_a_count_of_zero(tmp_path: Path) -> None:
    # A counter that could refuse to start would be a way for a bad path to
    # take the knowledge lane down, which is precisely the blast radius
    # RFC-001 section 10 bounds.
    missing = FileAllowanceStore(tmp_path / "not-there.json")
    assert missing.read() == ("", 0)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", "utf-8")
    assert FileAllowanceStore(corrupt).read() == ("", 0)


def test_a_file_store_rolls_over_rather_than_reading_last_month(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic-allowance.json"
    clock = FrozenClock(AUGUST)
    allowance = SemanticAllowance(limit=2, store=FileAllowanceStore(path), clock=clock)
    assert allowance.spend()
    assert allowance.spend()
    clock.moment = SEPTEMBER
    assert allowance.spend()
    assert FileAllowanceStore(path).read() == ("2026-09", 1)


@pytest.mark.parametrize("limit", [0, -1])
def test_an_allowance_of_nothing_grants_nothing(limit: int) -> None:
    allowance = SemanticAllowance(limit=limit, clock=FrozenClock(AUGUST))
    assert not allowance.spend()
    assert allowance.report().remaining == 0


def test_an_in_memory_store_can_be_seeded_for_a_test() -> None:
    store = InMemoryAllowanceStore("2026-08", 999)
    allowance = SemanticAllowance(limit=1000, store=store, clock=FrozenClock(AUGUST))
    assert allowance.spend()
    assert not allowance.spend()
