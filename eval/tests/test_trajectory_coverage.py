"""Can the shipped rows carry the numbers #74's report will make about them?

The clauses are checked against the set this repository ships rather than
against an example, for the same reason ``test_golden_coverage.py`` is: a
coverage check over a fixture is a check that the fixture is fine.
"""

from dataclasses import replace

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.trajectory.coverage import CLAUSES, RATE_NEEDS, coverage
from chip_chat.eval.trajectory.expectations import Expectation


def test_the_shipped_rows_meet_every_clause(rows: tuple[Expectation, ...]) -> None:
    """#74's scope, as something the dataset either supports or does not."""
    cover = coverage(rows)

    assert cover.complete, [clause.name for clause, _ in cover.unmet]
    assert cover.rows == len(rows)
    assert len(cover.met) == len(CLAUSES)


def test_every_lane_has_a_row_so_the_breakdown_has_no_holes(
    rows: tuple[Expectation, ...],
) -> None:
    """A lane with no rows is an absence the per-lane table cannot show."""
    cover = coverage(rows)

    assert cover.empty_lanes == ()
    assert dict(cover.per_lane).keys() == set(Lane)


def test_the_vision_lane_is_reported_as_thin(rows: tuple[Expectation, ...]) -> None:
    """One routing row on purpose, and the report has to say so.

    ``eval/photos`` is where the vision lane is scored; the golden set holds a
    single routing case and delegates the rest, which ``eval/README.md``
    explains. A lane that thin prints a percentage that reads like every other
    percentage unless something flags it.
    """
    cover = coverage(rows)

    assert Lane.VISION in cover.thin
    assert sum(1 for row in rows if row.lane is Lane.VISION) < RATE_NEEDS


def test_a_set_with_no_boundary_rows_is_refused() -> None:
    """Without them, *wrong lane* is a shape nothing in the set can demonstrate."""
    plain = tuple(replace(row, forbidden=frozenset()) for row in _one_of_each())

    cover = coverage(plain)

    assert not cover.complete
    assert any("boundary" in clause.name for clause, _ in cover.unmet)


def _one_of_each() -> tuple[Expectation, ...]:
    """One row per lane, with nothing else about them true."""
    return tuple(
        Expectation(entry_id=f"golden/{lane.value}", lane=lane, tool=None)
        for lane in Lane
    )
