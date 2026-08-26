"""That the randomness is addressed rather than shared.

Reproducibility is asserted end to end in ``test_determinism.py``. This file
is about the mechanism underneath it, because the failure it prevents is
invisible from the outside: a generator that draws every customer from one
shared stream is perfectly reproducible right up until someone changes a
parameter, at which point every customer after the first is a different person
for no reason anyone can see.
"""

import math

import pytest

from chip_chat.data_gen.rng import (
    jitter,
    palate,
    substream,
    weighted_choice,
    weighted_index,
    weighted_sample,
)


def test_the_same_address_is_the_same_stream() -> None:
    assert [substream(7, "customer", 3).random() for _ in range(1)] == [
        substream(7, "customer", 3).random()
    ]


def test_a_different_address_is_a_different_stream() -> None:
    """Including one that differs only in where the boundary between labels is."""
    first = substream(7, "customer", 3).random()

    assert substream(7, "customer", 4).random() != first
    assert substream(8, "customer", 3).random() != first
    assert substream(7, "customer3").random() != first
    assert substream(7, "custome", "r3").random() != first


def test_a_weighted_choice_respects_its_weights() -> None:
    counts = {"a": 0, "b": 0}
    for index in range(2000):
        counts[weighted_choice(substream(1, index), ["a", "b"], [0.8, 0.2])] += 1

    assert 0.75 < counts["a"] / 2000 < 0.85


def test_a_zero_weight_is_never_chosen() -> None:
    """A store with no traffic, an item with no palate: never, not rarely."""
    drawn = {
        weighted_choice(substream(2, index), ["a", "b", "c"], [1.0, 0.0, 1.0])
        for index in range(500)
    }

    assert drawn == {"a", "c"}


def test_a_distribution_with_no_mass_is_refused() -> None:
    with pytest.raises(ValueError, match="empty or massless"):
        weighted_index(substream(3), [0.0, 0.0])
    with pytest.raises(ValueError, match="empty or massless"):
        weighted_index(substream(3), [])


def test_mismatched_weights_are_refused() -> None:
    with pytest.raises(ValueError, match="2 items and 3 weights"):
        weighted_choice(substream(4), ["a", "b"], [1.0, 1.0, 1.0])


def test_a_sample_is_distinct_and_bounded_by_what_there_is() -> None:
    drawn = weighted_sample(substream(5), ["a", "b", "c"], [1.0, 1.0, 1.0], count=9)

    assert sorted(drawn) == ["a", "b", "c"]
    assert weighted_sample(substream(5), ["a"], [1.0], count=0) == []


def test_a_palate_is_a_distribution_and_a_peaked_one() -> None:
    """Below one, most of the mass lands on a few items. That is having favourites."""
    weights = palate(substream(6), 20, concentration=0.55)

    assert len(weights) == 20
    assert abs(math.fsum(weights) - 1.0) < 1e-9
    assert max(weights) > 2 / 20


def test_a_palate_at_concentration_one_has_no_favourites_worth_the_name() -> None:
    """The knob does what the config says it does."""
    peaked = [max(palate(substream(7, i), 30, 0.2)) for i in range(60)]
    flat = [max(palate(substream(7, i), 30, 3.0)) for i in range(60)]

    assert sum(peaked) / len(peaked) > sum(flat) / len(flat)


def test_an_empty_palate_is_empty_and_a_negative_one_is_refused() -> None:
    assert palate(substream(8), 0, 1.0) == ()
    with pytest.raises(ValueError, match="cannot cover"):
        palate(substream(8), -1, 1.0)
    with pytest.raises(ValueError, match="concentration must be positive"):
        palate(substream(8), 3, 0.0)


def test_jitter_averages_about_one_and_stretches_further_than_it_shrinks() -> None:
    """Log-normal, which is how real gaps between orders behave."""
    drawn = [jitter(substream(9, index), 0.4) for index in range(4000)]

    assert all(value > 0 for value in drawn)
    assert 0.95 < sum(drawn) / len(drawn) < 1.15
    assert max(drawn) - 1.0 > 1.0 - min(drawn)


def test_no_spread_is_a_metronome() -> None:
    assert jitter(substream(10), 0.0) == 1.0
