"""The baseline document: what it prints above the numbers, and what it refuses to.

A report on the headline metric has more ways to mislead than most, so each test
here is about one of them rather than about the Markdown.
"""

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.report import build_report, render
from chip_chat.eval.trajectory.run import run_trajectories
from chip_chat.eval.trajectory.testing import ScriptedSource, turn_spans
from chip_chat.eval.trajectory.trees import TraceSpan, Trajectory


def test_the_coverage_caveats_come_before_the_numbers(
    rows: tuple[Expectation, ...],
) -> None:
    """A thin lane prints a percentage that reads like every other percentage."""
    document = _render(rows, {})

    assert document.index("## Coverage") < document.index("## The metric")
    assert "Thin" in document
    assert Lane.VISION.value in document


def test_a_rate_that_does_not_exist_is_an_em_dash(
    rows: tuple[Expectation, ...],
) -> None:
    """Never a nought: an unscored lane has no accuracy, and 0% reads as failure."""
    document = _render(rows, {})

    assert "--" in document
    assert "0.0%" not in document


def test_a_missed_target_prints_the_gap_and_what_it_is_made_of(
    rows: tuple[Expectation, ...],
) -> None:
    """#74's third acceptance criterion: the gap explained, not only stated.

    The explanation is the shape, because the same number made of ``no_tool``
    and made of ``wrong_lane`` is two different problems.
    """
    document = _render(rows, {row.entry_id: turn_spans([]) for row in rows})

    assert "Target not met" in document
    assert "no_tool" in document
    assert "a gap of" in document


def test_a_split_trace_is_named_above_the_failures_with_its_issue(
    rows: tuple[Expectation, ...],
) -> None:
    """If the turn arrived as two traces, every number below is over a subset."""
    row = next(r for r in rows if r.tool is not None)
    assert row.tool is not None
    document = _render(rows, {row.entry_id: turn_spans([(row.tool, {})], split=True)})

    assert "could not be believed" in document
    assert "#103" in document
    assert "make trace-boundary" in document


def test_the_four_shapes_are_printed_with_what_they_mean(
    rows: tuple[Expectation, ...],
) -> None:
    """Four counts under one heading would read as four flavours of one problem."""
    document = _render(rows, {})

    for shape in ("wrong_lane", "no_tool", "extra_tools", "wrong_query"):
        assert f"**{shape}**" in document
    assert "groundedness" in document


def test_the_version_the_score_was_taken_against_is_in_the_document(
    rows: tuple[Expectation, ...],
) -> None:
    """The whole reason the register is a dataset rather than a manifest."""
    document = _render(rows, {})

    assert "abc123abc123" in document


def _render(
    rows: tuple[Expectation, ...], script: dict[str, tuple[TraceSpan, ...]]
) -> str:
    """Score a scripted run and render it."""
    trajectories: tuple[Trajectory, ...] = run_trajectories(
        rows, ScriptedSource(script=script)
    )
    return render(
        build_report(
            rows,
            trajectories,
            source="a fixture",
            dataset="cilantro-golden-set",
            version="abc123abc123",
        )
    )
