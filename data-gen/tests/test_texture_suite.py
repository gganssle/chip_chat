"""That the thinness check is a check, and that the shipped population passes it.

Two different claims, and the first is the load-bearing one. ``test_texture.py``
asserts that particular behaviours are in the data; this asserts that the
*suite* which asserts them can fail — a validation suite whose checks cannot
bite is worse than no suite at all, because it looks like the population was
checked. So most of what is here degenerates a population deliberately and
insists that the corresponding check notices.

Issue #28 is the ticket, and it is the gate on Phase 2: two merged tickets
(#25 and #26) both declined to claim food variety and both named this one as
where the claim would be made. It is made here, and made *relative to what the
catalogue makes possible*, which is the thing that lets it be made at all
against a nine-item fixture.
"""

import dataclasses
import re
from decimal import Decimal

import pytest
from population_fixtures import (
    fixture_catalog,
    fixture_population,
    fixture_terms,
    shipped_config,
    small_config,
    small_population,
)

from chip_chat.data_gen import (
    SEPARATING_MEASURES,
    SUPERLATIVES,
    TEXTURE_CHECKS,
    ConfigError,
    CustomerFacts,
    ThinPopulationError,
    generate_population,
    load_config,
    measure_texture,
    render_report,
)
from chip_chat.data_gen.texture import _BUILDERS

from regenerate_texture_report import REGENERATE, REPORT, document  # isort: skip


def texture():
    """Measure the shipped population once, for the tests that only read it."""
    return measure_texture(fixture_population(), fixture_catalog(), shipped_config())


# ---------------------------------------------------------------------------
# The claim itself.
# ---------------------------------------------------------------------------


def test_the_shipped_population_is_not_thin() -> None:
    """Issue #28's subject, in one assertion over every measure it names."""
    failures = texture().failures()

    assert not failures, "\n".join(f"{row.name}: {row.reads}" for row in failures)


def test_every_pair_of_archetypes_is_distinguishable() -> None:
    """ "Not three labels on the same distribution", as the statistic that says so.

    The check reports the *worst* pair, so asserting it is large is asserting
    it of all twenty-one. Complete separation is what the shipped population
    manages; the bound in the config is a large effect, with headroom.
    """
    separation = texture().check("persona_separation")

    assert separation.measured >= 0.8
    assert separation.held


def test_food_variety_is_measured_against_what_the_catalogue_publishes() -> None:
    """The resolution to what #25 and #26 both declined to claim.

    Coverage of the orderable menu rather than a count of foods, which is a
    claim about the generator rather than about Chipotle's menu, and therefore
    the same claim at nine items and at nine hundred.
    """
    measured = texture()

    assert measured.check("item_coverage").measured == 1.0
    assert measured.check("protein_coverage").measured == 1.0
    assert measured.orderable_items >= 1
    assert "orderable" in measured.check("item_coverage").reads


def test_every_ordered_line_resolves_to_a_published_catalogue_row() -> None:
    """The second half of the demo bar, asserted by the generator not by pytest."""
    assert texture().check("catalogue_resolution").measured == 1.0


def test_the_unknown_allergen_case_is_covered_as_far_as_the_catalogue_allows() -> None:
    """The coverage question the variety counts cannot see.

    ``NOT_LISTED`` does not mean "does not contain" and ``NOT_PUBLISHED``
    means nothing is known, so a population that only ever orders items with
    published allergen data never exercises the honest hedge. Measured against
    the catalogue because whether such an item exists to be ordered is
    Chipotle's decision: this fixture publishes allergen data for every
    orderable item and marks only ``Napkins & Utensils`` unpublished, which
    ``[catalogue].excluded_categories`` keeps out of baskets — so full
    coverage here is one state of one, and the report says which.
    """
    covered = texture().check("allergen_state_coverage")

    assert covered.measured == 1.0
    assert "PUBLISHED" in covered.reads


def test_the_generator_refuses_to_return_a_thin_population() -> None:
    """The suite runs on every generation, which is acceptance criterion one.

    Asserted by breaking the bound rather than by breaking the generator: a
    config demanding perfect store coverage from a population that cannot
    supply it must stop the run, and stop it in ``generate_population`` where
    nothing has been written yet.
    """
    config = _demanding("busiest_store_share", 0.001)

    with pytest.raises(ThinPopulationError) as failure:
        generate_population(fixture_catalog(), fixture_terms(), config)

    assert "busiest_store_share" in str(failure.value)
    assert "degenerate" in str(failure.value)


# ---------------------------------------------------------------------------
# That each check can bite. One degenerate population per property.
# ---------------------------------------------------------------------------


def measured(population, config=None):
    """Measure one population against the shipped config, or a given one."""
    return measure_texture(population, fixture_catalog(), config or shipped_config())


def test_a_population_where_everybody_orders_the_same_thing_fails() -> None:
    """The headline failure mode: one basket, five hundred customers.

    Every line rewritten to the same item with the same modifiers, which
    leaves the orders, the stores and the ledger exactly as they were. That is
    the point of doing it this way: this population is *only* thin in what was
    ordered, and the checks that should notice are the ones about food.
    """
    population = fixture_population()
    first = population.order_items[0]
    flattened = dataclasses.replace(
        population,
        order_items=tuple(
            dataclasses.replace(item, item_id=first.item_id, modifiers=first.modifiers)
            for item in population.order_items
        ),
    )

    failed = {check.name for check in measured(flattened).failures()}

    assert "item_coverage" in failed
    assert "palate_divergence" in failed
    assert "modifier_variety" in failed


def test_a_population_that_orders_one_invented_item_fails() -> None:
    """One hallucinated menu item, and the resolution check finds it.

    The failure the whole pipeline is arranged to make impossible, injected
    here because "impossible" is a claim that needs a test of its own.
    """
    population = fixture_population()
    invented = dataclasses.replace(
        population,
        order_items=(
            dataclasses.replace(population.order_items[0], item_id="CMG-NOT-A-THING"),
            *population.order_items[1:],
        ),
    )

    failed = measured(invented).check("catalogue_resolution")

    assert not failed.held
    assert failed.measured < 1.0


def test_a_modifier_from_a_different_item_does_not_resolve() -> None:
    """A real burrito with a real salsa off a different item is still invented."""
    population = fixture_population()
    other = next(
        item.modifiers[0]
        for item in population.order_items
        if item.modifiers
        if item.modifiers[0].split(":")[0] != population.order_items[0].item_id
    )
    grafted = dataclasses.replace(
        population,
        order_items=(
            dataclasses.replace(population.order_items[0], modifiers=(other,)),
            *population.order_items[1:],
        ),
    )

    assert not measured(grafted).check("catalogue_resolution").held


def test_a_population_where_one_store_takes_everything_fails() -> None:
    """Uneven is not the same as concentrated, and the ceiling is what says so."""
    population = fixture_population()
    single = dataclasses.replace(
        population,
        orders=tuple(
            dataclasses.replace(order, store_id=population.orders[0].store_id)
            for order in population.orders
        ),
    )

    failed = {check.name for check in measured(single).failures()}

    assert "busiest_store_share" in failed
    assert "store_coverage" in failed


def test_a_population_nobody_has_left_fails() -> None:
    """Nobody lapsed is nobody to surface stored value to."""
    population = fixture_population()
    still_here = dataclasses.replace(
        population,
        orders=tuple(
            dataclasses.replace(order, placed_at=population.window_ends_at)
            for order in population.orders
        ),
    )

    assert "lapsed_share" in {check.name for check in measured(still_here).failures()}


def test_a_population_that_all_spends_the_same_fails() -> None:
    """One mean, no lean: the shape the ticket asks spend not to have."""
    population = fixture_population()
    flat = dataclasses.replace(
        population,
        orders=tuple(
            dataclasses.replace(order, total=Decimal("10.00"))
            for order in population.orders
        ),
    )

    failed = {check.name for check in measured(flat).failures()}

    assert "spend_skew" in failed


def test_a_population_of_one_archetype_wearing_seven_names_fails() -> None:
    """The check the others cannot substitute for.

    Every customer keeps their history and their archetype label is
    reshuffled, so every distribution above is untouched and only the
    correspondence between label and behaviour is destroyed. A suite that
    measured spread alone would call this population healthy.
    """
    population = fixture_population()
    labels = [row.persona_id for row in population.demo_visitors]
    shuffled = dataclasses.replace(
        population,
        demo_visitors=tuple(
            dataclasses.replace(row, persona_id=labels[(index * 7 + 3) % len(labels)])
            for index, row in enumerate(population.demo_visitors)
        ),
    )

    separation = measured(shuffled).check("persona_separation")

    assert not separation.held
    assert separation.measured < 0.8


# ---------------------------------------------------------------------------
# The vocabulary, and the config that carries it.
# ---------------------------------------------------------------------------


def test_every_name_in_the_vocabulary_is_measured_exactly_once() -> None:
    """A name nothing measures is a bound that never bites."""
    names = [check.name for check in texture().checks]

    assert names == list(TEXTURE_CHECKS)
    assert len(_BUILDERS) == len(TEXTURE_CHECKS)


def test_every_check_carries_a_bound_from_the_shipped_config() -> None:
    """And every bound is the one in the file, not one in this module."""
    config = shipped_config()

    for check in texture().checks:
        assert check.bound == config.texture.bound(check.name)


def test_a_config_that_bounds_a_check_nobody_measures_is_refused(tmp_path) -> None:
    """The ordinary misspelling, caught at load rather than never."""
    broken = tmp_path / "unknown.toml"
    broken.write_text(
        _packaged().replace(
            "[texture.at_most]", "[texture.at_most]\nnot_a_check = 1.0\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="nothing measures"):
        load_config(broken)


def test_a_config_that_leaves_a_check_unbounded_is_refused(tmp_path) -> None:
    """The worse case: a measure that is reported and can never fail."""
    unbounded = tmp_path / "unbounded.toml"
    unbounded.write_text(
        re.sub(r"^persona_separation = .*$", "", _packaged(), flags=re.MULTILINE),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unbounded"):
        load_config(unbounded)


def test_a_config_that_bounds_a_check_twice_is_refused(tmp_path) -> None:
    """One check, one bound, one direction. Two is a bound nobody can read off."""
    twice = tmp_path / "twice.toml"
    twice.write_text(
        _packaged().replace(
            "[texture.at_most]", "[texture.at_most]\nstore_coverage = 1.0\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="repeats"):
        load_config(twice)


def test_the_two_usual_windows_may_not_overlap(tmp_path) -> None:
    """Otherwise a customer counts both as having a usual and as having none."""
    overlapping = tmp_path / "overlap.toml"
    overlapping.write_text(
        _packaged().replace("strong_usual_above = 0.60", "strong_usual_above = 0.10"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="strong_usual_above"):
        load_config(overlapping)


def test_every_separating_measure_is_a_fact_a_customer_carries() -> None:
    """A measure the facts do not carry would separate nobody from anybody."""
    fields = {field.name for field in dataclasses.fields(CustomerFacts)}

    assert set(SEPARATING_MEASURES) <= fields


# ---------------------------------------------------------------------------
# The standouts and the committed report.
# ---------------------------------------------------------------------------


def test_at_least_ten_distinct_customers_are_worth_a_demo_query() -> None:
    """Issue #28's third acceptance criterion, counted rather than asserted."""
    standouts = texture().standouts

    assert len({row.demo_id for row in standouts}) >= 10
    assert len(standouts) == len(SUPERLATIVES)


def test_every_standout_says_what_makes_them_interesting() -> None:
    """A list of customer ids is not "a one-line description of what makes them
    a good demo", which is what the ticket asks for."""
    for standout in texture().standouts:
        assert standout.superlative
        assert len(standout.description) > 30
        assert standout.demo_id.startswith("demo-")


def test_a_standout_is_a_customer_who_really_is_in_the_population() -> None:
    """And really is the archetype the row says they are."""
    minted = {row.demo_id: row.persona_id for row in fixture_population().demo_visitors}

    for standout in texture().standouts:
        assert minted[standout.demo_id] == standout.persona_id


def test_the_committed_report_is_the_one_this_population_produces() -> None:
    """Compared without being rewritten, so a stale document fails rather than heals.

    The same arrangement ``test_catalog_fixture.py`` uses for the committed
    catalogue, and for the same reason: a generated artefact that is only
    regenerated when somebody remembers is an artefact describing whatever was
    true when they last remembered. Compared *in memory* on purpose — a test
    that regenerated the file first would prove only that the generator is
    deterministic, and would quietly fix the staleness it was meant to catch.
    """
    committed = REPORT.read_text(encoding="utf-8")

    assert committed == document(), f"{REPORT.name} is out of date; run {REGENERATE}"


def test_the_report_renders_every_check_and_every_distribution() -> None:
    """The criterion is "a report showing each distribution", so each one is in it."""
    measurement = texture()

    rendered = render_report(measurement, fixture_population(), "Texture")

    for check in measurement.checks:
        assert check.name in rendered
        assert check.asks in rendered
    for spread in measurement.spreads:
        assert spread.name in rendered
    for standout in measurement.standouts:
        assert standout.demo_id in rendered


def test_a_failing_report_says_so_in_its_verdict() -> None:
    """A report that read the same whether or not the checks held would be decoration."""
    population = fixture_population()
    broken = measure_texture(
        population, fixture_catalog(), _demanding("store_coverage", 2.0)
    )

    rendered = render_report(broken, population, "Texture")

    assert "checks failed" in rendered
    assert "✗" in rendered


# ---------------------------------------------------------------------------
# That the suite is not a property of the population's size.
# ---------------------------------------------------------------------------


def test_a_sixty_customer_population_clears_the_same_bounds() -> None:
    """Scale-free bounds, asserted at a scale the shipped ones were not tuned at.

    Not a formality: a suite whose bounds only hold at five hundred customers
    would fail every test in this package that generates a smaller one, and
    would be a suite about the shipped config rather than about the generator.
    """
    assert not measured(small_population(), small_config()).failures()


def _demanding(check: str, bound: float):
    """Return the shipped config with one texture bound moved past reach."""
    config = shipped_config()
    at_least = tuple(
        (name, bound if name == check else value)
        for name, value in config.texture.at_least
    )
    at_most = tuple(
        (name, bound if name == check else value)
        for name, value in config.texture.at_most
    )
    return dataclasses.replace(
        config,
        texture=dataclasses.replace(config.texture, at_least=at_least, at_most=at_most),
    )


def _packaged() -> str:
    """Return the shipped ``population.toml`` as text, for tests that break it."""
    from population_fixtures import PACKAGED

    return PACKAGED.read_text(encoding="utf-8")
