"""Whether the population is thin, measured rather than asserted.

Issue #28, and it is the claim two earlier tickets deliberately declined to
make. Issue #25 shipped the generator saying "proving the population is not
thin against a real catalogue is #28 — this catalogue has two entrees, so
variety of *food* is not assertable here"; issue #26 said the same about the
persona fixtures. Both were right to refuse, and both left the claim owned by
nobody. This module owns it.

Trap 1 in the system design is thin synthetic data: if every customer looks
the same, personalization has nothing to find and the demo has nothing to
show. The trap is quiet — a thin population generates, writes, validates
referentially and passes every test in this package — so the only thing that
catches it is a measurement, taken on every run, with a number it has to
clear.

**Every measure is relative to what the catalogue makes possible.** That is
the resolution to the problem the two earlier tickets ran into. "Twelve
different entrees were ordered" is a claim about the harvest, not about the
generator, and a suite asserting it would fail against the committed fixture
catalogue for a reason that is nobody's fault. "Every entree the catalogue
publishes was ordered by somebody" is a claim about the generator, it holds at
nine orderable items and at nine hundred, and it *bites* on a real harvest
where a generator that only ever composes three baskets would sail past an
absolute threshold. So the checks below are coverages, ratios, shares and
effect sizes, and the report prints the ceiling beside every one of them:
where the catalogue is the limit, the report says the catalogue is the limit
rather than quietly crediting the generator for it.

**The bounds are in ``population.toml``, like every other number.** They are
part of what "not thin" means for this product, which makes them a product
decision — ``[texture]`` sits beside ``[personas]`` for the same reason the
archetypes do. A check whose bound is missing from the file is a
:class:`~chip_chat.data_gen.errors.ConfigError`, never a check that quietly
passes: a criterion misspelt into inertness is exactly how a population ends
up certified by a suite that measured nothing.

**Two of these are not distributional at all**, and they are here because the
system design's demo bar is one sentence with two halves — *"a query that
surfaces a genuinely interesting customer, whose every order is a real menu
item."* ``catalogue_resolution`` is the second half asserted mechanically
over every line of every order, on every generation rather than only under
pytest, and ``allergen_state_coverage`` is the coverage question underneath it:
the catalogue models three allergen states — ``CONTAINS``, ``NOT_LISTED`` and
``NOT_PUBLISHED``, where ``NOT_LISTED`` explicitly does *not* mean "does not
contain" — and a population that only ever orders items with published
allergen data would look healthy on every variety count while never once
exercising the unknown case, which is the case that matters downstream.
"""

import math
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from chip_chat.catalog import MenuCatalog
from chip_chat.data_gen.catalogue import OrderableMenu
from chip_chat.data_gen.config import (
    MEASURES,
    TEXTURE_CHECKS,
    GeneratorConfig,
    TextureConfig,
)
from chip_chat.data_gen.errors import ThinPopulationError
from chip_chat.data_gen.fixtures import CustomerFacts, entree_ids, measure_customers
from chip_chat.data_gen.records import Channel, SyntheticPopulation

CHECKS = TEXTURE_CHECKS
"""Every check this module measures, re-exported from the config vocabulary.

The names live in :mod:`chip_chat.data_gen.config` because they are keys a
person writes in ``population.toml``, and they are closed for the reason
:data:`~chip_chat.data_gen.config.MEASURES` is: a bound naming a check that
does not exist is a bound that never bites, and a suite made of bounds that
never bite certifies nothing while looking like it does. The reader refuses an
unknown name and a missing one alike, and ``test_texture_suite.py`` asserts
that this module measures exactly this list — no more, and none fewer.
"""

SEPARATING_MEASURES = MEASURES
"""What two archetypes may be told apart *by*.

Deliberately the whole of
:data:`~chip_chat.data_gen.config.MEASURES` rather than a chosen few, and
deliberately the same list a fixture may be selected on. The question
``persona_separation`` asks is whether the archetypes are seven
distinguishable populations or seven labels on one, and narrowing what counts
as a distinguishing feature would be answering it in advance: two archetypes
that differ only in *when* they order are still two archetypes.

Its order is load-bearing, weakly. Most pairs in the shipped population are
completely separated on several measures at once, and the one the report names
is the earliest here among them — a declared order rather than an alphabetical
accident, so a retune that does not change the outcome does not change the
sentence either.
"""


@dataclass(frozen=True, slots=True)
class Check:
    """One measured property of the population, and the bar it had to clear.

    Attributes:
        name: One of :data:`CHECKS`.
        asks: The question this check puts to the population, in one line.
        measured: What the population scored.
        bound: What it had to score, from ``[texture]``.
        at_least: Whether :attr:`bound` is a floor. ``False`` is a ceiling.
        reads: The measurement as a sentence, with whatever context makes it
            interpretable — the ceiling the catalogue sets, the pair of
            archetypes that separated worst, the state nobody ordered.
    """

    name: str
    asks: str
    measured: float
    bound: float
    at_least: bool
    reads: str

    @property
    def held(self) -> bool:
        """Return whether the population cleared the bound."""
        if self.at_least:
            return self.measured >= self.bound
        return self.measured <= self.bound


@dataclass(frozen=True, slots=True)
class Spread:
    """One distribution, summarised and bucketed so it can be printed.

    Quantiles rather than a mean and a standard deviation, because the whole
    question is whether the distribution has a shape, and a mean is what you
    report when you have already assumed it does not.

    Attributes:
        name: What was measured.
        unit: What one of them is — customers, orders, lines.
        count: How many were measured.
        smallest: The minimum.
        p10: The tenth percentile.
        median: The fiftieth.
        p90: The ninetieth.
        largest: The maximum.
        mean: The arithmetic mean, for comparison against the median. Far
            apart is the skew this population is supposed to have.
        buckets: A histogram as ``(label, count)`` pairs, ascending.
    """

    name: str
    unit: str
    count: int
    smallest: float
    p10: float
    median: float
    p90: float
    largest: float
    mean: float
    buckets: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Standout:
    """One customer worth building a demo query around.

    Issue #28's third acceptance criterion asks for "at least ten hand-picked
    interesting customers, each with a one-line description of what makes them
    a good demo". These are picked by machine rather than by hand, which is
    the only version of the criterion that survives a retune: a customer
    hand-listed in a document is a customer who stops being interesting the
    day ``palate_concentration`` moves, and nothing says so.

    They are also *not* ``persona_fixtures``. That table answers "which
    customer should a visitor be assigned to demonstrate this archetype", and
    it is answered archetype by archetype. This answers "which customer would
    make somebody lean forward", and it is answered across the whole
    population — the largest unclaimed balance in it belongs to whoever it
    belongs to, and which archetype they were minted as is a fact about them
    rather than a slot they were chosen to fill.

    Attributes:
        superlative: What they are the extreme of, which is the reason they
            are on the list.
        demo_id: Which customer.
        persona_id: Which archetype they turned out to be.
        description: One line, written from their own measured columns.
    """

    superlative: str
    demo_id: str
    persona_id: str
    description: str


@dataclass(frozen=True, slots=True)
class Texture:
    """Everything measured about one population's variety.

    Attributes:
        checks: Every check, in :data:`CHECKS` order.
        spreads: The distributions behind them, for the report.
        standouts: The customers worth a demo query.
        customers: How many customers were measured.
        orders: How many orders they placed.
        lines: How many order lines those carried.
        orderable_items: How many distinct things the catalogue makes
            orderable — the ceiling on food variety, printed everywhere it is
            relevant so that a small number is read as a small catalogue and
            not as a thin generator.
    """

    checks: tuple[Check, ...]
    spreads: tuple[Spread, ...]
    standouts: tuple[Standout, ...]
    customers: int
    orders: int
    lines: int
    orderable_items: int

    def failures(self) -> tuple[Check, ...]:
        """Return every check the population did not clear, in order."""
        return tuple(check for check in self.checks if not check.held)

    def check(self, name: str) -> Check:
        """Return one check by name.

        Args:
            name: One of :data:`CHECKS`.

        Returns:
            The check.

        Raises:
            KeyError: If ``name`` is not a check.
        """
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(f"no such check {name!r}; expected one of {CHECKS}")

    def spread(self, name: str) -> Spread:
        """Return one distribution by name.

        Args:
            name: What was measured.

        Returns:
            The distribution.

        Raises:
            KeyError: If nothing of that name was measured.
        """
        for spread in self.spreads:
            if spread.name == name:
                return spread
        raise KeyError(f"no such distribution {name!r}")


def measure_texture(
    population: SyntheticPopulation, catalog: MenuCatalog, config: GeneratorConfig
) -> Texture:
    """Measure how much variety one population actually has.

    Measures rather than judges: every check carries what it scored and what
    it had to score, and a caller wanting the verdict asks
    :meth:`Texture.failures`. :func:`check_texture` is the one that raises.

    Args:
        population: The generated population.
        catalog: The catalogue it was composed from. Read for the ceiling on
            food variety and for the allergen disclosure of what was ordered.
        config: The tuned parameters, whose ``[texture]`` carries the bounds.

    Returns:
        The measurement.
    """
    menu = OrderableMenu(catalog, config.catalogue)
    facts = measure_customers(
        population.demo_visitors,
        population.orders,
        population.order_items,
        population.loyalty_ledger,
        entree_ids(menu),
        population.window_ends_at,
    )
    taken = _Taken(
        population=population,
        catalog=catalog,
        config=config,
        menu=menu,
        facts=facts,
        basket=_baskets(population),
        mix=_mixes(population, catalog, menu),
        orderable=_orderable(menu),
    )

    checks = tuple(builder(taken) for builder in _BUILDERS)
    return Texture(
        checks=checks,
        spreads=_spreads(facts, taken.basket),
        standouts=_standouts(facts),
        customers=len(facts),
        orders=len(population.orders),
        lines=len(population.order_items),
        orderable_items=len(taken.orderable),
    )


def check_texture(
    population: SyntheticPopulation, catalog: MenuCatalog, config: GeneratorConfig
) -> Texture:
    """Measure the population and refuse it if it is thin.

    Called by :func:`~chip_chat.data_gen.generate.generate_population`, so
    every generation is checked and a degenerate one never reaches a file. The
    alternative — a suite run separately, when someone remembers — is a suite
    that certifies the population somebody generated last week.

    Args:
        population: The generated population.
        catalog: The catalogue it was composed from.
        config: The tuned parameters.

    Returns:
        The measurement, when every check held.

    Raises:
        ThinPopulationError: If any check did not. The message names every
            failure, what it measured and what it needed, because "the
            population is thin" is not actionable and "eleven per cent of the
            catalogue was never ordered" is.
    """
    texture = measure_texture(population, catalog, config)
    failed = texture.failures()
    if failed:
        raise ThinPopulationError(
            "the generated population is degenerate on "
            f"{len(failed)} of {len(texture.checks)} measures:\n"
            + "\n".join(
                f"  {check.name}: {check.reads} "
                f"({'at least' if check.at_least else 'at most'} "
                f"{check.bound:g} required)"
                for check in failed
            )
        )
    return texture


# ---------------------------------------------------------------------------
# The checks. One function each, taking the same measured inputs, so that the
# list of what is checked reads as a list.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Baskets:
    """Per-order and per-line shapes, gathered in one pass.

    Attributes:
        quantities: Items in each order, one entry per order.
        modifiers: Modifiers on each line, one entry per line.
    """

    quantities: tuple[int, ...]
    modifiers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Mixes:
    """What each customer ordered, and what everybody ordered.

    Both are counts over *orderable item identifiers* rather than over lines:
    a modifier is a thing the customer chose, and a palate that shows up as
    "always guacamole" is invisible to anything counting only entrees.

    Attributes:
        population: How often each item was ordered, over everybody.
        customers: The same, per customer.
        unresolved: Lines naming an item or modifier the catalogue does not
            publish. Zero, always, and counted rather than assumed.
    """

    population: Mapping[str, int]
    customers: Mapping[str, Mapping[str, int]]
    unresolved: int


@dataclass(frozen=True, slots=True)
class _Taken:
    """Everything the checks read, gathered once and handed to all of them.

    One argument rather than eight, so that the list of what is checked reads
    as a list: every check below is one function of this, and the difference
    between two of them is what they ask rather than what they were passed.

    Attributes:
        population: The generated population.
        catalog: The catalogue it was composed from.
        config: The tuned parameters, whose ``[texture]`` carries the bounds.
        menu: The orderable view of the catalogue.
        facts: Every customer, measured.
        basket: The per-order and per-line shapes.
        mix: What each customer ordered, and what everybody did.
        orderable: Every identifier the catalogue makes orderable, to its
            published name. The ceiling on food variety.
    """

    population: SyntheticPopulation
    catalog: MenuCatalog
    config: GeneratorConfig
    menu: OrderableMenu
    facts: tuple[CustomerFacts, ...]
    basket: _Baskets
    mix: _Mixes
    orderable: Mapping[str, str]


def _order_frequency(taken: _Taken) -> Check:
    """Not everyone is a weekly regular."""
    counts = [float(row.order_count) for row in taken.facts]
    ratio = _ratio(counts)
    return _at_least(
        taken.config.texture,
        "order_frequency_ratio",
        "Do customers order at genuinely different rates?",
        ratio,
        f"the busiest tenth of customers placed {ratio:.1f}x the orders of the "
        f"quietest tenth ({_quantile(counts, 0.9):.0f} against "
        f"{_quantile(counts, 0.1):.0f})",
    )


def _item_coverage(taken: _Taken) -> Check:
    """Everything the catalogue offers was ordered by somebody."""
    ordered = set(taken.mix.population) & set(taken.orderable)
    share = len(ordered) / len(taken.orderable) if taken.orderable else 0.0
    missed = sorted(taken.orderable[key] for key in set(taken.orderable) - ordered)
    tail = f"; never ordered: {', '.join(missed)}" if missed else ""
    return _at_least(
        taken.config.texture,
        "item_coverage",
        "Does the population reach the whole orderable menu?",
        share,
        f"{len(ordered)} of the {len(taken.orderable)} things this catalogue makes "
        f"orderable were ordered{tail}",
    )


def _protein_coverage(taken: _Taken) -> Check:
    """Protein choice varies, over whatever proteins are published."""
    fillings = {
        item.item_id: item.primary_filling
        for item in taken.catalog.menu_items
        if item.primary_filling
    }
    published = set(fillings.values())
    chosen = {
        filling
        for item_id, filling in fillings.items()
        if taken.mix.population.get(item_id, 0) > 0
    }
    share = len(chosen) / len(published) if published else 0.0
    return _at_least(
        taken.config.texture,
        "protein_coverage",
        "Is more than one protein ever chosen?",
        share,
        f"{len(chosen)} of the {len(published)} proteins the catalogue "
        f"publishes were ordered ({', '.join(sorted(chosen))})",
    )


def _palate_divergence(taken: _Taken) -> Check:
    """There is something for ``item_affinity`` to learn.

    The long tail the ticket asks for is here rather than in the aggregate
    popularity curve, and deliberately. Aggregate concentration is capped by
    the catalogue — nine orderable items cannot produce a long tail whatever
    the customers do — while what a recommender actually learns is the gap
    between one customer's mix and everybody's. That gap is scale-free: it is
    zero when every customer orders the population average, which is exactly
    the thin population this check exists to catch, and it is large when the
    palate is doing its job.
    """
    keys = sorted(taken.mix.population)
    total = sum(taken.mix.population.values()) or 1
    overall = [taken.mix.population[key] / total for key in keys]
    divergences = sorted(
        _divergence([counts.get(key, 0) for key in keys], overall)
        for counts in taken.mix.customers.values()
    )
    median = _quantile(divergences, 0.5)
    gini = _gini([taken.mix.population[key] for key in keys])
    return _at_least(
        taken.config.texture,
        "palate_divergence",
        "Does one customer's taste differ from the population's?",
        median,
        f"a typical customer's item mix sits {median:.3f} bits from the "
        f"population's, the widest {max(divergences, default=0.0):.3f} "
        f"(aggregate popularity gini {gini:.2f} over "
        f"{len(taken.orderable)} orderable items)",
    )


def _usual_spread(taken: _Taken) -> Check:
    """``usual_order`` confidence varies meaningfully across the population."""
    shares = [row.usual_share for row in taken.facts]
    spread = _quantile(shares, 0.9) - _quantile(shares, 0.1)
    return _at_least(
        taken.config.texture,
        "usual_share_spread",
        "Does how predictable a customer is vary across the population?",
        spread,
        f"usual_share runs from {_quantile(shares, 0.1):.2f} at the tenth "
        f"percentile to {_quantile(shares, 0.9):.2f} at the ninetieth, a "
        f"spread of {spread:.2f}",
    )


def _without_a_usual(taken: _Taken) -> Check:
    """Some customers genuinely don't have one, which is the honest-hedge path."""
    ceiling = taken.config.texture.no_usual_above
    share = _share(taken.facts, lambda row: row.usual_share <= ceiling)
    return _at_least(
        taken.config.texture,
        "without_a_usual",
        "Do some customers genuinely have no usual order?",
        share,
        f"{share:.1%} of customers repeat their commonest basket at most "
        f"{ceiling:.0%} of the time, so there is nothing to reorder for them",
    )


def _with_a_usual(taken: _Taken) -> Check:
    """And some emphatically do, or the one-turn reorder has nobody to run on."""
    floor = taken.config.texture.strong_usual_above
    share = _share(taken.facts, lambda row: row.usual_share >= floor)
    return _at_least(
        taken.config.texture,
        "with_a_usual",
        "Do some have one dominant enough to reorder in a single turn?",
        share,
        f"{share:.1%} of customers repeat their commonest basket at least "
        f"{floor:.0%} of the time",
    )


def _basket_size_ratio(taken: _Taken) -> Check:
    """Basket sizes vary: one bowl and a floor's lunch are both in here."""
    sizes = [float(value) for value in taken.basket.quantities]
    ratio = _ratio(sizes)
    return _at_least(
        taken.config.texture,
        "basket_size_ratio",
        "Do basket sizes vary between one bowl and a group order?",
        ratio,
        f"the largest tenth of baskets carry {ratio:.1f}x the items of the "
        f"smallest tenth ({_quantile(sizes, 0.9):.0f} against "
        f"{_quantile(sizes, 0.1):.0f}), the biggest "
        f"{max(sizes, default=0.0):.0f}",
    )


def _modifier_variety(taken: _Taken) -> Check:
    """Modifier counts vary, so a bowl is not always built the same way.

    Measured as the share of lines built with a different *number* of
    modifiers than the commonest, rather than as an entropy: the count of
    distinguishable builds is capped by how many slots the catalogue
    publishes, and a share is not.
    """
    counted = Counter(taken.basket.modifiers)
    total = sum(counted.values())
    commonest, many = counted.most_common(1)[0] if counted else (0, 0)
    share = (total - many) / total if total else 0.0
    return _at_least(
        taken.config.texture,
        "modifier_variety",
        "Is the same item built differently by different people?",
        share,
        f"{share:.1%} of lines carry a number of modifiers other than the "
        f"commonest ({commonest}), across "
        f"{len(counted)} distinct builds",
    )


def _store_coverage(taken: _Taken) -> Check:
    """No store on the roster is a store nobody has ever ordered from."""
    roster = taken.menu.stores(taken.catalog.stores, taken.config.stores)
    ordered = {order.store_id for order in taken.population.orders}
    share = len(ordered & {store.store_id for store in roster}) / len(roster)
    return _at_least(
        taken.config.texture,
        "store_coverage",
        "Was every store on the roster ordered from?",
        share,
        f"{len(ordered)} of the {len(roster)} stores on the roster took at "
        "least one order",
    )


def _store_concentration(taken: _Taken) -> Check:
    """Traffic is uneven, because a population with no busy store has none to name."""
    counted = Counter(order.store_id for order in taken.population.orders)
    shares = sorted(counted.values(), reverse=True)
    ratio = shares[0] / shares[-1] if shares and shares[-1] else 0.0
    return _at_least(
        taken.config.texture,
        "store_concentration",
        "Is traffic uneven across the stores?",
        ratio,
        f"the busiest store took {ratio:.1f}x the orders of the quietest "
        f"({shares[0] if shares else 0} against {shares[-1] if shares else 0})",
    )


def _busiest_store_share(taken: _Taken) -> Check:
    """And uneven *plausibly*: busiest, not the only one anybody uses.

    The other half of ``store_concentration``, and the reason both are
    here. A ratio alone is cleared just as well by one store taking every
    order as by thirty stores with a plausible head, and the first of those is
    a thin population wearing the shape of a varied one.
    """
    counted = Counter(order.store_id for order in taken.population.orders)
    total = sum(counted.values()) or 1
    share = max(counted.values(), default=0) / total
    return _at_most(
        taken.config.texture,
        "busiest_store_share",
        "Is the busiest store busy rather than the only one?",
        share,
        f"the busiest of {len(counted)} stores took {share:.1%} of all orders",
    )


def _lapsed_share(taken: _Taken) -> Check:
    """A meaningful fraction of customers are lapsed.

    Measured on silence rather than on the ``lapsed`` label, which is the
    version of the question the assistant can actually see: nothing
    downstream reads ``persona_id``, and a population whose lapsed customers
    are only lapsed by assignment has nobody to surface stored value to.
    """
    days = taken.config.texture.lapsed_after_days
    share = _share(taken.facts, lambda row: row.days_since_order >= days)
    return _at_least(
        taken.config.texture,
        "lapsed_share",
        "Has a meaningful fraction of customers gone quiet?",
        share,
        f"{share:.1%} of customers have not ordered for {days} days or more",
    )


def _new_share(taken: _Taken) -> Check:
    """And a meaningful fraction are new, on the same measured footing."""
    days = taken.config.texture.new_within_days
    share = _share(taken.facts, lambda row: row.days_since_first_order <= days)
    return _at_least(
        taken.config.texture,
        "new_share",
        "Has a meaningful fraction only started recently?",
        share,
        f"{share:.1%} of customers placed their first order within the last {days} days",
    )


def _spend_ratio(taken: _Taken) -> Check:
    """Spend is not clustered around a single mean."""
    spends = [float(row.lifetime_spend) for row in taken.facts]
    ratio = _ratio(spends)
    return _at_least(
        taken.config.texture,
        "spend_ratio",
        "Does lifetime spend span more than one order of magnitude?",
        ratio,
        f"the top tenth of customers spent {ratio:.1f}x the bottom tenth "
        f"(${_quantile(spends, 0.9):,.0f} against "
        f"${_quantile(spends, 0.1):,.0f})",
    )


def _spend_skew(taken: _Taken) -> Check:
    """ "Not normal around a single mean", said as the statistic that means it.

    A normal distribution has zero skew. A spend distribution with a long
    right tail — a few office managers carrying a floor's lunch, most people
    carrying a bowl — has a positive one, and the ratio above cannot tell the
    two apart on its own: a symmetric distribution wide enough clears it too.
    """
    spends = [float(row.lifetime_spend) for row in taken.facts]
    skew = _skew(spends)
    return _at_least(
        taken.config.texture,
        "spend_skew",
        "Does spend lean, rather than cluster symmetrically?",
        skew,
        f"spend skews {skew:+.2f}, mean ${_mean(spends):,.0f} against median "
        f"${_quantile(spends, 0.5):,.0f}",
    )


def _persona_separation(taken: _Taken) -> Check:
    """The archetypes are distinguishable populations, not labels on one.

    This is the check the ticket is really about, and the one the others
    cannot substitute for: a population can have a wide spread on every
    measure above and still be one undifferentiated blob with seven names
    written on it, which is a demo where switching persona changes nothing a
    visitor can see.

    Every *pair* of archetypes is scored, and the check reports the worst
    pair, because a suite that averaged them would let five well-separated
    pairs hide a sixth that is indistinguishable. A pair is scored on the best
    it can do across :data:`SEPARATING_MEASURES` — two archetypes that differ
    only in when they order are still two archetypes.

    The statistic is Cliff's delta: the probability that a customer drawn from
    one archetype outranks one drawn from the other, minus the reverse. Zero
    is two samples from the same distribution; one is complete separation. It
    is used here rather than a difference of means because it is scale-free
    and does not assume either distribution has a shape — which is the whole
    thing under test.
    """
    grouped: dict[str, list[CustomerFacts]] = {}
    for row in taken.facts:
        grouped.setdefault(row.persona_id, []).append(row)

    scored = [
        (
            *_separates(grouped[left], grouped[right]),
            f"{left} against {right}",
        )
        for left, right in combinations(sorted(grouped), 2)
    ]
    worst, measure, pair = min(
        scored, default=(1.0, "nothing", "one archetype, so nothing to compare")
    )
    where = f"{pair}, told apart by {measure}"
    return _at_least(
        taken.config.texture,
        "persona_separation",
        "Can every pair of archetypes be told apart?",
        worst,
        f"the worst-separated of the {len(grouped) * (len(grouped) - 1) // 2} "
        f"archetype pairs scores {worst:.2f} on Cliff's delta ({where})",
    )


def _separates(
    left: Sequence[CustomerFacts], right: Sequence[CustomerFacts]
) -> tuple[float, str]:
    """Return how far apart two archetypes are, and on which measure.

    The best any measure manages, because two archetypes that differ only in
    when they order are still two archetypes. Ties break towards the earlier
    name in :data:`SEPARATING_MEASURES`, which is a declared order rather than
    an alphabetical accident — with the shipped config most pairs are
    completely separated on several measures at once, and the one reported
    should be stable across a retune that does not change the outcome.
    """
    best, chosen = 0.0, SEPARATING_MEASURES[0]
    for name in SEPARATING_MEASURES:
        delta = abs(
            _cliffs_delta(
                [row.measure(name) for row in left],
                [row.measure(name) for row in right],
            )
        )
        if delta > best:
            best, chosen = delta, name
    return best, chosen


def _catalogue_resolution(taken: _Taken) -> Check:
    """Every order is a real menu item, asserted on every generation.

    The second half of the system design's demo bar. It is already asserted
    over the fixture population by ``test_referential_integrity.py``, and that
    is a different claim: that test says the generator composed real food from
    a nine-item fixture on the machine that ran pytest. This says it about
    *this* population, composed from *this* catalogue, on the run that wrote
    it — which is the run a demo will be given from.
    """
    total = len(taken.population.order_items)
    resolved = total - taken.mix.unresolved
    share = resolved / total if total else 0.0
    return _at_least(
        taken.config.texture,
        "catalogue_resolution",
        "Does every ordered line resolve to a published catalogue row?",
        share,
        f"{resolved:,} of {total:,} order lines name an item and modifiers the "
        "catalogue publishes",
    )


def _allergen_states(taken: _Taken) -> Check:
    """The unknown allergen case is exercised, when the catalogue has one.

    ``NOT_LISTED`` does not mean "does not contain", and ``NOT_PUBLISHED``
    means nothing whatever is known — so a population that only ever orders
    items with published allergen data looks healthy on every variety count
    above while never once putting the honest "Chipotle does not publish this"
    answer in front of the assistant. That is thinness in the coverage of the
    case that matters most and the easiest kind to miss.

    Measured against the catalogue rather than against the three states in the
    abstract, for the reason every other check here is: whether a
    ``NOT_PUBLISHED`` food exists to be ordered is Chipotle's decision, not
    the generator's. What the generator owes is to order across whatever
    states the orderable menu carries, and that is what is scored.
    """
    disclosures = {
        item.item_id: item.allergen_disclosure.value
        for item in taken.catalog.menu_items
        if item.item_id in taken.orderable
    }
    offered = set(disclosures.values())
    exercised = {
        state
        for item_id, state in disclosures.items()
        if taken.mix.population.get(item_id, 0) > 0
    }
    share = len(exercised) / len(offered) if offered else 0.0
    missed = sorted(offered - exercised)
    tail = f"; never ordered: {', '.join(missed)}" if missed else ""
    return _at_least(
        taken.config.texture,
        "allergen_state_coverage",
        "Is every allergen-disclosure state on the orderable menu ordered?",
        share,
        f"{len(exercised)} of the {len(offered)} allergen-disclosure states "
        f"this catalogue's orderable items carry were ordered "
        f"({', '.join(sorted(exercised))}){tail}",
    )


_BUILDERS: tuple[Callable[[_Taken], Check], ...] = (
    _order_frequency,
    _item_coverage,
    _protein_coverage,
    _palate_divergence,
    _usual_spread,
    _without_a_usual,
    _with_a_usual,
    _basket_size_ratio,
    _modifier_variety,
    _store_coverage,
    _store_concentration,
    _busiest_store_share,
    _lapsed_share,
    _new_share,
    _spend_ratio,
    _spend_skew,
    _persona_separation,
    _catalogue_resolution,
    _allergen_states,
)
"""The checks, in :data:`CHECKS` order. ``test_texture_suite.py`` asserts they
agree, so a check added here and forgotten there is a failure rather than a
name in a vocabulary that nothing measures."""


# ---------------------------------------------------------------------------
# The standouts: ten-plus customers worth building a demo query around.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Superlative:
    """One way of being the most interesting customer in the population.

    Attributes:
        title: What this customer is the extreme of.
        measure: Which measured fact to rank on.
        biggest: Whether the extreme is the largest value or the smallest.
        describe: A sentence written from the chosen customer's own columns.
        eligible: An extra condition, so that "the widest palate" is not won
            by somebody with three orders.
    """

    title: str
    measure: str
    biggest: bool
    describe: Callable[[CustomerFacts], str]
    eligible: Callable[[CustomerFacts], bool] = lambda row: True


SUPERLATIVES: tuple[_Superlative, ...] = (
    _Superlative(
        "The largest unclaimed balance",
        "points_balance",
        True,
        lambda row: (
            f"{row.points_balance:,} points sitting unspent after "
            f"{row.order_count} orders — the single biggest stored value in "
            f"the population, and last seen {_days(row.days_since_order)} ago"
        ),
    ),
    _Superlative(
        "The most predictable customer",
        "usual_share",
        True,
        lambda row: (
            f"{row.usual_share:.0%} of {row.order_count} orders are the "
            "identical basket — the one-turn reorder has nobody better to run "
            "on"
        ),
        lambda row: row.order_count >= 20,
    ),
    _Superlative(
        "The least predictable customer",
        "usual_share",
        False,
        lambda row: (
            f"{row.distinct_baskets} different baskets in {row.order_count} "
            f"orders and no basket above {row.usual_share:.0%} — the customer "
            'the assistant has to say "I am not sure what your usual is" to'
        ),
        lambda row: row.order_count >= 20,
    ),
    _Superlative(
        "The biggest spender",
        "lifetime_spend",
        True,
        lambda row: (
            f"${row.lifetime_spend:,.0f} across {row.order_count} orders, "
            f"averaging {row.entrees_per_order:.1f} entrees a time"
        ),
    ),
    _Superlative(
        "The most travelled",
        "distinct_stores",
        True,
        lambda row: (
            f"{row.distinct_stores} different stores in {row.order_count} "
            f"orders, with only {row.store_share:.0%} at their commonest — "
            "there is no such thing as their store"
        ),
    ),
    _Superlative(
        "The most loyal to one store",
        "store_share",
        True,
        lambda row: (
            f"{row.store_share:.0%} of {row.order_count} orders at store "
            f"{row.favourite_store}, over "
            f"{_days(row.days_since_first_order - row.days_since_order)}"
        ),
        lambda row: row.order_count >= 30,
    ),
    _Superlative(
        "The biggest group orderer",
        "entrees_per_order",
        True,
        lambda row: (
            f"{row.entrees_per_order:.1f} entrees per order across "
            f"{row.order_count} orders — somebody is ordering for a floor"
        ),
    ),
    _Superlative(
        "The longest gone",
        "days_since_order",
        True,
        lambda row: (
            f"silent for {_days(row.days_since_order)} after "
            f"{row.order_count} orders, with {row.points_balance:,} points "
            "still on the card"
        ),
    ),
    _Superlative(
        "The newest customer with real history",
        "days_since_first_order",
        False,
        lambda row: (
            f"first ordered {_days(row.days_since_first_order)} ago and has "
            f"placed {row.order_count} orders since — new, and not empty"
        ),
        lambda row: row.order_count >= 5,
    ),
    _Superlative(
        "The most frequent",
        "order_count",
        True,
        lambda row: (
            f"{row.order_count} orders in the window, ${row.lifetime_spend:,.0f} "
            f"in all, {row.usual_share:.0%} of them the same basket"
        ),
    ),
    _Superlative(
        "The widest range of baskets",
        "distinct_baskets",
        True,
        lambda row: (
            f"{row.distinct_baskets} distinct baskets — more different orders "
            f"than most customers have orders"
        ),
    ),
    _Superlative(
        "The rarest visitor with points to spend",
        "order_count",
        False,
        lambda row: (
            f"only {row.order_count} orders in eighteen months, and "
            f"{row.points_balance:,} points nobody has reminded them about"
        ),
        lambda row: row.points_balance >= 500,
    ),
)
"""Twelve ways of being worth a demo query, ranked over the whole population.

Twelve rather than ten because two customers may win two superlatives, and
issue #28's floor is ten *customers*. They are cross-cutting on purpose: none
of them mentions an archetype, so which kind of customer holds the largest
unclaimed balance is a finding about the population rather than a slot filled
by design. ``test_texture_suite.py`` asserts the shipped population yields at
least ten distinct ones.
"""


def _days(count: int) -> str:
    """Return a day count as English, so that a description never reads "1 days"."""
    return "1 day" if count == 1 else f"{count:,} days"


def _standouts(facts: Sequence[CustomerFacts]) -> tuple[Standout, ...]:
    """Return the extreme customer for each superlative, ties broken by id."""
    chosen: list[Standout] = []
    for superlative in SUPERLATIVES:
        eligible = [row for row in facts if superlative.eligible(row)]
        if not eligible:
            continue
        direction = -1.0 if superlative.biggest else 1.0
        row = min(
            eligible,
            key=lambda fact: (
                direction * fact.measure(superlative.measure),
                fact.demo_id,
            ),
        )
        chosen.append(
            Standout(
                superlative=superlative.title,
                demo_id=row.demo_id,
                persona_id=row.persona_id,
                description=superlative.describe(row),
            )
        )
    return tuple(chosen)


# ---------------------------------------------------------------------------
# The distributions the report prints.
# ---------------------------------------------------------------------------


def _spreads(facts: Sequence[CustomerFacts], basket: _Baskets) -> tuple[Spread, ...]:
    """Summarise every distribution issue #28 asks to see rendered."""
    return (
        _spread("orders per customer", "customers", [row.order_count for row in facts]),
        _spread(
            "lifetime spend", "customers", [float(row.lifetime_spend) for row in facts]
        ),
        _spread("items per order", "orders", list(basket.quantities)),
        _spread("modifiers per line", "lines", list(basket.modifiers)),
        _spread("usual_share", "customers", [row.usual_share for row in facts]),
        _spread("distinct baskets", "customers", [row.distinct_baskets for row in facts]),
        _spread("distinct stores", "customers", [row.distinct_stores for row in facts]),
        _spread("points balance", "customers", [row.points_balance for row in facts]),
        _spread(
            "days since last order", "customers", [row.days_since_order for row in facts]
        ),
    )


BUCKETS = 8
"""How many buckets a histogram in the report carries. Enough to show a shape,
few enough to read in a terminal."""


def _spread(name: str, unit: str, raw: Iterable[float]) -> Spread:
    """Summarise one distribution, histogram included."""
    values = sorted(float(value) for value in raw)
    if not values:
        return Spread(name, unit, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ())
    return Spread(
        name=name,
        unit=unit,
        count=len(values),
        smallest=values[0],
        p10=_quantile(values, 0.1),
        median=_quantile(values, 0.5),
        p90=_quantile(values, 0.9),
        largest=values[-1],
        mean=_mean(values),
        buckets=_histogram(values),
    )


def _histogram(values: Sequence[float]) -> tuple[tuple[str, int], ...]:
    """Return equal-width buckets over ``values``, ascending.

    Equal width rather than equal count, because equal-count buckets have the
    same height by construction and would hide the very shape the report is
    printed to show.
    """
    low, high = values[0], values[-1]
    if high <= low:
        return ((_label(low, high), len(values)),)
    width = (high - low) / BUCKETS
    counted = [0] * BUCKETS
    for value in values:
        index = min(int((value - low) / width), BUCKETS - 1)
        counted[index] += 1
    return tuple(
        (_label(low + index * width, low + (index + 1) * width), many)
        for index, many in enumerate(counted)
    )


def _label(low: float, high: float) -> str:
    """Return one bucket's label, at a precision the numbers deserve."""
    if high - low >= 1 or high >= 100:
        return f"{low:,.0f}-{high:,.0f}"
    return f"{low:.2f}-{high:.2f}"


# ---------------------------------------------------------------------------
# Gathering: one pass each over the orders, the lines and the customers.
# ---------------------------------------------------------------------------


def _baskets(population: SyntheticPopulation) -> _Baskets:
    """Return the per-order and per-line shapes in one pass over the lines."""
    quantities: Counter[str] = Counter()
    modifiers: list[int] = []
    for item in population.order_items:
        quantities[item.order_id] += item.qty
        modifiers.append(len(item.modifiers))
    return _Baskets(
        quantities=tuple(quantities[order.order_id] for order in population.orders),
        modifiers=tuple(modifiers),
    )


def _mixes(
    population: SyntheticPopulation, catalog: MenuCatalog, menu: OrderableMenu
) -> _Mixes:
    """Return what each customer ordered, what everybody did, and what did not resolve.

    A modifier counts as a thing the customer chose, under the identifier of
    the item it adds — ``guacamole on a burrito`` and ``guacamole on a bowl``
    are the same taste and should not read as two. Anything that fails to
    resolve is counted rather than raised on, because the count is the
    ``catalogue_resolution`` check and a check that crashes reports
    nothing at all.

    A modifier resolves only when it is a modifier *of the item it is on*: the
    catalogue keys them ``(item_id, modifier_item_id)``, and a real burrito
    with an invented salsa is the same failure wearing a smaller hat.
    """
    owner = {order.order_id: order.demo_id for order in population.orders}
    published = {item.item_id for item in catalog.menu_items}
    overall: Counter[str] = Counter()
    per_customer: dict[str, Counter[str]] = {}
    unresolved = 0
    for item in population.order_items:
        demo_id = owner.get(item.order_id, "")
        mine = per_customer.setdefault(demo_id, Counter())
        chosen = [item.item_id] if item.item_id in published else []
        held = item.item_id in published
        for modifier_id in item.modifiers:
            try:
                modifier = menu.modifier(modifier_id)
            except KeyError:
                held = False
                continue
            held = held and modifier.item_id == item.item_id
            chosen.append(modifier.modifier_item_id)
        unresolved += 0 if held else 1
        for key in chosen:
            overall[key] += item.qty
            mine[key] += item.qty
    return _Mixes(population=overall, customers=per_customer, unresolved=unresolved)


def _orderable(menu: OrderableMenu) -> dict[str, str]:
    """Return every identifier the catalogue makes orderable, to its name.

    The ceiling on food variety, and the denominator of
    ``item_coverage``. Entrees, their modifiers, their paid extras, sides
    and drinks, on either channel — a thing sold only at the counter is still
    a thing the population should have ordered.
    """
    names: dict[str, str] = {}
    for channel in Channel:
        for buildable in menu.entrees(channel):
            names[buildable.item.item_id] = buildable.item.name
            for slot in (*buildable.required, *buildable.optional):
                for modifier in slot.choices:
                    names[modifier.modifier_item_id] = modifier.name
            for modifier in buildable.extras:
                names[modifier.modifier_item_id] = modifier.name
        for item in (*menu.sides(channel), *menu.drinks(channel)):
            names[item.item_id] = item.name
    return names


# ---------------------------------------------------------------------------
# The report: the distributions rendered, so that "the data is interesting"
# is visible rather than asserted.
# ---------------------------------------------------------------------------

BAR = 46
"""How many characters the tallest bar in a histogram is drawn with."""


def render_report(texture: Texture, population: SyntheticPopulation, title: str) -> str:
    """Render the measurement as Markdown, histograms included.

    Markdown with text histograms rather than a notebook, for two reasons that
    are both about review. A committed notebook is a JSON blob whose diff is
    unreadable, so a retune that flattened a distribution would land as a
    thousand changed lines that nobody reads; this diffs as the numbers that
    moved. And a notebook has to be *run* to show anything, which makes the
    committed artefact a promise about output rather than the output.

    Args:
        texture: The measurement.
        population: The population it was taken from, for the digests that say
            which population this report is of.
        title: The document's heading.

    Returns:
        The report, ending in a newline.
    """
    failed = texture.failures()
    lines = [
        f"# {title}",
        "",
        f"**Population** `{population.version()[:16]}` · seed `{population.seed}` · "
        f"catalogue `{population.catalog_content_version[:16]}` · "
        f"rewards `{population.rewards_content_version[:16]}`",
        "",
        f"{texture.customers:,} customers · {texture.orders:,} orders · "
        f"{texture.lines:,} order lines · "
        f"{texture.orderable_items} orderable things on the menu",
        "",
        "Generated by `chip_chat.data_gen.texture`. Do not edit by hand: run",
        "`uv run python data-gen/tests/regenerate_texture_report.py` and commit",
        "the diff.",
        "",
        "## Verdict",
        "",
        (
            f"**Every one of the {len(texture.checks)} checks holds.**"
            if not failed
            else f"**{len(failed)} of {len(texture.checks)} checks failed.**"
        ),
        "",
        "Each check is measured relative to what this catalogue makes possible, so the",
        "same bounds are meaningful against a nine-item fixture and against a real",
        "harvest. The bounds themselves are in `[texture]` in `population.toml`.",
        "",
        "| Check | Asks | Measured | Bound | |",
        "| --- | --- | ---: | ---: | :-: |",
    ]
    for check in texture.checks:
        direction = "≥" if check.at_least else "≤"
        lines.append(
            f"| `{check.name}` | {check.asks} | {check.measured:,.3f} | "
            f"{direction} {check.bound:,.3f} | {'✓' if check.held else '✗'} |"
        )

    lines += ["", "### What each one measured", ""]
    for check in texture.checks:
        lines.append(f"- **`{check.name}`** — {check.reads}.")

    lines += ["", "## The distributions", ""]
    for spread in texture.spreads:
        lines += _rendered(spread)

    lines += [
        f"## {len(texture.standouts)} customers worth a demo query",
        "",
        "Issue #28's third criterion. Picked by machine over the whole population",
        "rather than by hand, so that a retune moves the list instead of quietly",
        "invalidating it, and cross-cutting rather than per-archetype — which kind of",
        "customer holds the largest unclaimed balance is a finding, not a slot filled by",
        "design. That is what makes these different from `persona_fixtures`, which",
        "answers the other question: which particular customer a visitor is assigned",
        "in order to demonstrate an archetype.",
        "",
        "| What makes them interesting | Customer | Minted as | Why they demo well |",
        "| --- | --- | --- | --- |",
    ]
    for standout in texture.standouts:
        lines.append(
            f"| {standout.superlative} | `{standout.demo_id}` | "
            f"`{standout.persona_id}` | {standout.description} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _bar(count: int, tallest: int) -> str:
    """Return one histogram bar, never empty for a bucket that has something in it."""
    if not count:
        return ""
    return "\u2588" * max(1, round(BAR * count / tallest))


def _rendered(spread: Spread) -> list[str]:
    """Return one distribution as a heading, a quantile line and a histogram."""
    if not spread.count:
        return [f"### {spread.name}", "", "_Nothing measured._", ""]
    tallest = max(count for _, count in spread.buckets) or 1
    width = max(len(label) for label, _ in spread.buckets)
    body = [
        f"{label:>{width}} | {_bar(count, tallest)} {count:,}"
        for label, count in spread.buckets
    ]
    return [
        f"### {spread.name}",
        "",
        f"{spread.count:,} {spread.unit} · min {spread.smallest:,.2f} · "
        f"p10 {spread.p10:,.2f} · median {spread.median:,.2f} · "
        f"p90 {spread.p90:,.2f} · max {spread.largest:,.2f} · "
        f"mean {spread.mean:,.2f}",
        "",
        "```",
        *body,
        "```",
        "",
    ]


# ---------------------------------------------------------------------------
# Statistics. Stdlib only, and each one written out rather than imported, so
# that what a number means is readable beside the check that uses it.
# ---------------------------------------------------------------------------


def _at_least(
    texture: TextureConfig, name: str, asks: str, measured: float, reads: str
) -> Check:
    """Return a check whose bound is a floor."""
    return Check(
        name=name,
        asks=asks,
        measured=measured,
        bound=texture.bound(name),
        at_least=True,
        reads=reads,
    )


def _at_most(
    texture: TextureConfig, name: str, asks: str, measured: float, reads: str
) -> Check:
    """Return a check whose bound is a ceiling."""
    return Check(
        name=name,
        asks=asks,
        measured=measured,
        bound=texture.bound(name),
        at_least=False,
        reads=reads,
    )


def _share(
    facts: Sequence[CustomerFacts], holds: Callable[[CustomerFacts], bool]
) -> float:
    """Return the share of customers a predicate holds for."""
    if not facts:
        return 0.0
    return sum(1 for row in facts if holds(row)) / len(facts)


def _ratio(values: Sequence[float]) -> float:
    """Return the ninetieth percentile over the tenth, or zero if the tenth is."""
    low = _quantile(values, 0.1)
    return _quantile(values, 0.9) / low if low else 0.0


def _quantile(values: Sequence[float], fraction: float) -> float:
    """Return one quantile by linear interpolation between order statistics."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    below, above = math.floor(position), math.ceil(position)
    if below == above:
        return ordered[below]
    return ordered[below] * (above - position) + ordered[above] * (position - below)


def _mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean, or zero for nothing."""
    return sum(values) / len(values) if values else 0.0


def _skew(values: Sequence[float]) -> float:
    """Return Pearson's moment coefficient of skewness.

    Zero for any symmetric distribution, positive for a long right tail. This
    is the statistic that distinguishes "spread out" from "not normal around a
    single mean", which are different claims and only the second is what issue
    #28 asks for.
    """
    if len(values) < 3:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    if variance <= 0:
        return 0.0
    third = sum((value - mean) ** 3 for value in values) / len(values)
    return third / variance**1.5


def _gini(counts: Sequence[int]) -> float:
    """Return the Gini coefficient of a popularity curve.

    Reported rather than checked. Its ceiling is set by how many things the
    catalogue publishes and by how many of them the published menu makes
    compulsory — a bowl always has rice — so a bound on it would be a bound on
    the harvest. ``palate_divergence`` is the checkable form of the same
    question.
    """
    ordered = sorted(counts)
    total = sum(ordered)
    if not ordered or total <= 0:
        return 0.0
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    return (2 * weighted - (len(ordered) + 1) * total) / (len(ordered) * total)


def _divergence(counts: Sequence[int], overall: Sequence[float]) -> float:
    """Return the Jensen-Shannon divergence of one mix from the population's, in bits.

    Zero when a customer orders exactly the population average, which is the
    thin population this measure exists to catch, and at most one when they
    share nothing at all. Symmetric and always finite, unlike the
    Kullback-Leibler divergence it is built from — a customer who never orders
    something everybody else does would otherwise be infinitely divergent, and
    that customer is ordinary.
    """
    total = sum(counts)
    if total <= 0:
        return 0.0
    mine = [value / total for value in counts]
    middle = [(left + right) / 2 for left, right in zip(mine, overall, strict=True)]
    return (_relative(mine, middle) + _relative(overall, middle)) / (2 * math.log(2))


def _relative(left: Sequence[float], right: Sequence[float]) -> float:
    """Return the Kullback-Leibler divergence of ``left`` from ``right``, in nats."""
    return sum(
        value * math.log(value / other)
        for value, other in zip(left, right, strict=True)
        if value > 0
        if other > 0
    )


def _cliffs_delta(left: Sequence[float], right: Sequence[float]) -> float:
    """Return Cliff's delta between two samples.

    The probability that a value drawn from ``left`` exceeds one drawn from
    ``right``, minus the reverse. Zero means the two samples are
    indistinguishable by this measure; one means every value of ``left``
    exceeds every value of ``right``. Ties count for neither side, which is
    what makes it honest about a measure that takes few distinct values.
    """
    if not left or not right:
        return 0.0
    ordered = sorted(right)
    greater = smaller = 0
    for value in left:
        smaller += bisect_left(ordered, value)
        greater += len(ordered) - bisect_right(ordered, value)
    return (smaller - greater) / (len(left) * len(ordered))
