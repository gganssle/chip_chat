"""Where every random number in this package comes from, and why not just one.

The generator's first acceptance criterion is that the same seed produces
byte-identical output. One shared stream would satisfy that only while nothing
ever changed: draw the customers from it in a loop and the four hundredth
customer's history depends on how many numbers the three hundred and ninety
ninth happened to consume, so raising ``toppings_max`` would silently rewrite
everybody. A population that cannot be retuned without rewriting itself is not
retunable, which is what issue #25's fourth criterion actually asks for.

So streams are addressed rather than shared. :func:`substream` derives one
from the seed and a label — ``("customer", "demo-0214", "orders")`` — through
SHA-256, which means every draw is a pure function of the seed and of the
thing being drawn for. Customers are independent of each other, and of the
order they are generated in.
"""

import hashlib
import math
import random
from collections.abc import Sequence

_KEY_SEPARATOR = b"\x1f"
"""ASCII unit separator. Not a character any label contains, so ``("ab", "c")``
and ``("a", "bc")`` cannot derive the same stream."""


def substream(seed: int, *labels: object) -> random.Random:
    """Return the stream addressed by ``seed`` and ``labels``.

    Args:
        seed: The population seed.
        labels: What this stream is for. Stringified, so integers and
            :class:`~enum.StrEnum` members are as good as strings.

    Returns:
        A generator seeded by the digest of the whole address. Two calls with
        the same arguments return streams that produce the same numbers.
    """
    digest = hashlib.sha256(
        _KEY_SEPARATOR.join(
            [str(seed).encode("utf-8"), *(str(label).encode("utf-8") for label in labels)]
        )
    ).digest()
    return random.Random(int.from_bytes(digest, "big"))


def weighted_index(rng: random.Random, weights: Sequence[float]) -> int:
    """Return an index into ``weights``, chosen in proportion to them.

    Written out rather than delegated to :func:`random.choices` so that the
    number of draws it costs is one, visibly and forever: the streams above
    are only independent if the draws inside them are stable.

    Args:
        rng: The stream to draw from.
        weights: Non-negative weights. Need not sum to one.

    Returns:
        The chosen index.

    Raises:
        ValueError: If ``weights`` is empty or has no mass in it.
    """
    total = math.fsum(weights)
    if not weights or total <= 0.0:
        raise ValueError("cannot choose from an empty or massless distribution")
    threshold = rng.random() * total
    running = 0.0
    for index, weight in enumerate(weights):
        running += weight
        if running > threshold:
            return index
    return len(weights) - 1


def weighted_choice[T](
    rng: random.Random, items: Sequence[T], weights: Sequence[float]
) -> T:
    """Return one item, chosen in proportion to ``weights``.

    Args:
        rng: The stream to draw from.
        items: What to choose from.
        weights: One weight per item.

    Returns:
        The chosen item.

    Raises:
        ValueError: If the lengths differ, or nothing could be chosen.
    """
    if len(items) != len(weights):
        raise ValueError(f"{len(items)} items and {len(weights)} weights")
    return items[weighted_index(rng, weights)]


def weighted_sample[T](
    rng: random.Random, items: Sequence[T], weights: Sequence[float], count: int
) -> list[T]:
    """Return ``count`` distinct items, drawn in proportion to ``weights``.

    Without replacement, because two of the same topping is not a choice the
    published menu offers — an extra portion is, and the catalogue publishes
    it as its own modifier.

    Args:
        rng: The stream to draw from.
        items: What to choose from.
        weights: One weight per item.
        count: How many to draw. Clamped to the number of items.

    Returns:
        The drawn items, in the order they were drawn.

    Raises:
        ValueError: If the lengths differ, or ``count`` is negative.
    """
    if len(items) != len(weights):
        raise ValueError(f"{len(items)} items and {len(weights)} weights")
    if count < 0:
        raise ValueError(f"cannot draw {count} items")
    remaining = list(items)
    left = list(weights)
    drawn: list[T] = []
    for _ in range(min(count, len(remaining))):
        if math.fsum(left) <= 0.0:  # pragma: no cover - palates are positive
            break
        index = weighted_index(rng, left)
        drawn.append(remaining.pop(index))
        left.pop(index)
    return drawn


def palate(rng: random.Random, size: int, concentration: float) -> tuple[float, ...]:
    """Return ``size`` weights describing one customer's preferences.

    A draw from a symmetric Dirichlet distribution, which is what the
    normalised gamma variates below are. The concentration is the whole point:
    at one the weights are uniform over the simplex and a customer likes
    everything equally, and below one most of the mass lands on a few items —
    which is what having favourites means, and what ``item_affinity`` and
    ``usual_order`` need to exist before they can find anything.

    Args:
        rng: The stream to draw from.
        size: How many items the customer has an opinion about.
        concentration: The Dirichlet concentration. Must be positive.

    Returns:
        ``size`` weights summing to one, in the order the items were given.

    Raises:
        ValueError: If ``size`` is negative or ``concentration`` is not
            positive.
    """
    if size < 0:
        raise ValueError(f"a palate cannot cover {size} items")
    if concentration <= 0.0:
        raise ValueError(f"concentration must be positive, got {concentration}")
    if size == 0:
        return ()
    drawn = [rng.gammavariate(concentration, 1.0) for _ in range(size)]
    total = math.fsum(drawn)
    if total <= 0.0:  # pragma: no cover - every gamma variate is positive
        return tuple(1.0 / size for _ in range(size))
    return tuple(value / total for value in drawn)


def jitter(rng: random.Random, spread: float) -> float:
    """Return a positive multiplier averaging about one.

    Log-normal, so a gap between orders can stretch further than it can shrink
    — which is how real gaps behave, and why a symmetric jitter would produce
    a population whose cadences all look the same at the tails.

    Args:
        rng: The stream to draw from.
        spread: The log-scale standard deviation. Zero is a metronome.

    Returns:
        The multiplier.
    """
    if spread <= 0.0:
        return 1.0
    return math.exp(rng.normalvariate(0.0, spread))
