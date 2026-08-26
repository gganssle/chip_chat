"""That every persona fixture demonstrates the thing it exists to demonstrate.

Issue #26's second acceptance criterion is the whole of this file: *"each
archetype's defining behaviour is verified by an assertion, not by
inspection."* So nothing below reads a narrative and nods at it. Each of the
three PRD personas is checked against the measurement PRD section 02 names for
it, and checked *relative to the others* wherever a bare threshold would be a
number pulled out of the air:

- **The Regular** — turns-to-reorder, target one. Needs a dominant usual, so
  the assertion is that their commonest basket really does dominate, and by a
  wide margin over the Explorer's.
- **The Lapsed Customer** — whether stored value is surfaced before they ask.
  Needs unredeemed points *and* months of silence, so both are asserted, and
  the ledger is asserted to show them walking away leaving the points behind.
- **The Explorer** — whether answers are hedged appropriately. Needs a *low*
  confidence usual, which is a feature; the assertion is that no basket of
  theirs comes close to dominating.

The cold-start risk of PRD section 06 is what all of this is guarding, and the
ticket is blunt about the standard: "if a fixture cannot demonstrate its own
metric, it is not finished".

Two things this file does not assert. **Variety of food**: the committed
fixture catalogue has two entrees in it, so a fixture's orders can be varied in
structure and not in ingredient, and a count here would be a test of the
fixture. Issue #28 asks that question in the form that survives a small
catalogue — coverage of what the catalogue makes orderable — in
``test_texture_suite.py``. What is asserted here is variety of *behaviour*.
**Rewards correctness**: points balances are
this generator's provisional arithmetic and are reconciled against Chipotle's
published terms by issue #27. What is asserted is that stored value exists to
be surfaced, never that it is the right amount.
"""

import dataclasses
import re
from collections import Counter
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest
from population_fixtures import (
    PACKAGED,
    fixture_catalog,
    fixture_population,
    fixture_terms,
    shipped_config,
    small_config,
    small_population,
)

from chip_chat.catalog import Store
from chip_chat.data_gen import (
    MEASURES,
    PUBLISHED_BOUNDS,
    PUBLISHED_READERS,
    Bound,
    ConfigError,
    CustomerFacts,
    OrderableMenu,
    PersonaFixture,
    SyntheticPopulation,
    entree_ids,
    load_config,
    measure_customers,
    resolve,
    select_fixtures,
)

PRD_PERSONAS = ("regular", "lapsed", "explorer")
"""The three PRD section 02 names, which are also the three the demo assigns."""


@cache
def orderable() -> OrderableMenu:
    """Return the orderable view of the fixture catalogue."""
    return OrderableMenu(fixture_catalog(), shipped_config().catalogue)


@cache
def stores() -> tuple[Store, ...]:
    """Return the stores the shipped population orders from."""
    return orderable().stores(fixture_catalog().stores, shipped_config().stores)


def measured(population: SyntheticPopulation) -> dict[str, CustomerFacts]:
    """Return one population's measured customers, by ``demo_id``."""
    return {
        row.demo_id: row
        for row in measure_customers(
            population.demo_visitors,
            population.orders,
            population.order_items,
            population.loyalty_ledger,
            entree_ids(orderable()),
            population.window_ends_at,
        )
    }


@cache
def facts() -> dict[str, CustomerFacts]:
    """Return the shipped population's measured customers, by ``demo_id``."""
    return measured(fixture_population())


def fixtures_for(persona_id: str) -> tuple[PersonaFixture, ...]:
    """Return one archetype's fixtures, best first."""
    return tuple(
        row
        for row in fixture_population().persona_fixtures
        if row.persona_id == persona_id
    )


# --------------------------------------------------------------------------
# Acceptance criterion 1: a query surfaces a genuinely interesting customer for
# each archetype, and every one of their orders is a real menu item.
# --------------------------------------------------------------------------


def test_every_archetype_supplies_more_than_three_fixtures() -> None:
    """ "More than three concrete instances of each archetype."

    The ticket's reason is persona switching: three fixtures means a demo that
    switches personas shows the same three accounts. An archetype falling short
    here has customers who cannot demonstrate it, which is the cold-start risk
    of PRD section 06 arriving quietly.
    """
    config = shipped_config()
    counts = Counter(row.persona_id for row in fixture_population().persona_fixtures)

    assert config.fixtures_per_persona > 3
    for spec in config.personas:
        assert counts[spec.persona_id] == config.fixtures_per_persona, (
            f"{spec.persona_id} supplied {counts[spec.persona_id]} fixtures; its "
            "customers do not demonstrate it well enough to fill the roster"
        )


def test_a_fixture_is_a_real_customer_with_the_history_it_claims() -> None:
    """Every column is a fact about the population, not a decoration."""
    population = fixture_population()
    visitors = {row.demo_id: row for row in population.demo_visitors}
    counts: Counter[str] = Counter(order.demo_id for order in population.orders)
    spend: dict[str, Decimal] = {}
    for order in population.orders:
        spend[order.demo_id] = spend.get(order.demo_id, Decimal("0")) + order.total

    for row in population.persona_fixtures:
        assert visitors[row.demo_id].persona_id == row.persona_id
        assert counts[row.demo_id] == row.order_count
        assert spend[row.demo_id] == row.lifetime_spend
        assert row.first_order_at <= row.last_order_at
        assert (
            row.days_since_order == (population.window_ends_at - row.last_order_at).days
        )


def test_a_fixtures_usual_is_a_real_menu_item_built_from_real_modifiers() -> None:
    """ "Every one of their orders is a real menu item."

    The population-wide version of this is ``test_referential_integrity.py``.
    Asserted again over the fixtures on purpose: these are the rows a visitor is
    actually shown, and a usual is resolved by a different path from the orders
    it was derived from.
    """
    catalog = fixture_catalog()
    items = {item.item_id for item in catalog.menu_items}
    modifiers = {row.modifier_id: row.item_id for row in catalog.modifiers}

    for row in fixture_population().persona_fixtures:
        assert row.usual_item_id in items
        for modifier_id in row.usual_modifiers:
            assert modifiers[modifier_id] == row.usual_item_id, (
                f"{row.demo_id}'s usual carries {modifier_id}, which the "
                "catalogue publishes for a different item"
            )


def test_a_fixtures_store_is_one_it_actually_orders_from() -> None:
    """``home_store`` is derived from orders, not inherited from the archetype.

    RFC-001 section 04 is explicit that ``customer_360.favourite_store`` comes
    from ``orders.store_id`` and may legitimately disagree with a stated
    preference. The narrative names this store, so it has to be the one the
    customer really uses.
    """
    population = fixture_population()
    published = {store.store_id for store in stores()}
    counted: dict[str, Counter[int]] = {}
    for order in population.orders:
        counted.setdefault(order.demo_id, Counter())[order.store_id] += 1

    for row in population.persona_fixtures:
        counts = counted[row.demo_id]
        assert row.home_store in published
        assert counts[row.home_store] == max(counts.values())
        assert row.store_share == counts[row.home_store] / counts.total()
        assert row.distinct_stores == len(counts)


# --------------------------------------------------------------------------
# Acceptance criterion 2: each archetype's defining behaviour is verified by an
# assertion. One test per PRD persona, plus the group orderer.
# --------------------------------------------------------------------------


def test_the_regular_can_reorder_in_one_turn() -> None:
    """PRD section 02: measured by turns-to-reorder, where the target is one.

    One turn is only reachable if there is no ambiguity about what to reorder,
    so what is asserted is the dominance itself: the same basket over and over
    for most of eighteen months, at one store, recently enough to still be a
    habit.
    """
    for row in fixtures_for("regular"):
        assert row.usual_share >= 0.85, (
            f"{row.demo_id} is offered as the Regular but only "
            f"{row.usual_share:.0%} of their orders are the same basket; there "
            "is no single thing for a one-turn reorder to mean"
        )
        assert row.usual_item_id is not None
        assert row.order_count >= 30
        assert row.store_share >= 0.90
        assert row.days_since_order <= 60


def test_the_lapsed_customer_has_stored_value_to_surface() -> None:
    """PRD requirement P3 needs something to surface, unprompted.

    Both halves are asserted. Points without the silence is a customer who was
    in last week, and silence without the points is a customer with nothing to
    be reminded of — neither demonstrates P3.
    """
    threshold = fixture_terms().costliest.point_cost
    for row in fixtures_for("lapsed"):
        assert row.days_since_order >= 90, (
            f"{row.demo_id} is offered as the Lapsed Customer but ordered "
            f"{row.days_since_order} days before the window closed"
        )
        assert row.points_balance >= threshold, (
            f"{row.demo_id} has {row.points_balance} points, short of the "
            f"{threshold} the costliest published reward costs; that is stored "
            "value but not the kind worth interrupting someone about"
        )


def test_the_lapsed_customer_really_did_leave_the_points_behind() -> None:
    """The balance is unredeemed, not merely computed.

    "Points they have forgotten about" is a claim about the *ledger*: it must
    show them earning, then stopping, with the balance still standing at the
    end. A customer who redeemed on their way out has forgotten nothing.
    """
    population = fixture_population()
    ledger: dict[str, list] = {}
    for entry in population.loyalty_ledger:
        ledger.setdefault(entry.demo_id, []).append(entry)

    for row in fixtures_for("lapsed"):
        entries = sorted(ledger[row.demo_id], key=lambda entry: entry.created_at)
        assert sum(entry.delta for entry in entries) == row.points_balance
        assert entries[-1].created_at <= row.last_order_at
        assert (population.window_ends_at - entries[-1].created_at).days >= 90


def test_the_explorer_has_no_usual_and_that_is_the_point() -> None:
    """PRD section 02: the Explorer must produce a *low*-confidence usual.

    A feature, not a defect — it is the fixture that exercises the honest
    "I am not sure what your usual is" path. So the assertion is inverted: no
    basket of theirs may come close to dominating, and they must have ordered
    enough different things for a recommender to have signal to work with.
    """
    for row in fixtures_for("explorer"):
        assert row.usual_share <= 0.15, (
            f"{row.demo_id} is offered as the Explorer but {row.usual_share:.0%} "
            "of their orders are the same basket; a confident usual is exactly "
            "what this fixture must not have"
        )
        assert row.distinct_baskets >= 20
        assert row.order_count >= 20
        assert row.distinct_stores >= 5


def test_the_regular_and_the_explorer_are_not_the_same_customer() -> None:
    """The two are read from opposite ends of one measure, so the gap must be wide.

    Asserted as a relation rather than as two thresholds: retuning
    ``population.toml`` should be free to move both numbers without breaking the
    claim that these are different kinds of person. A margin this size is also
    what lets the ``usual_order`` mart compute confidence its own way and still
    agree about which of them has one.
    """
    regulars = fixtures_for("regular")
    explorers = fixtures_for("explorer")

    assert min(row.usual_share for row in regulars) > 4 * max(
        row.usual_share for row in explorers
    )
    assert min(row.distinct_baskets for row in explorers) > max(
        row.distinct_baskets for row in regulars
    )
    assert min(row.store_share for row in regulars) > max(
        row.store_share for row in explorers
    )


def test_the_group_orderer_is_already_in_the_data() -> None:
    """Issue #26: out of scope for V0, but the data must not need regenerating.

    "A fourth — the group orderer assembling an office lunch — is out of scope
    for V0 but should shape the generated data so that nothing has to be
    regenerated when it arrives." A fixture that already demonstrates
    multi-entree ordering is that shaping, checked.
    """
    group = fixtures_for("office_manager")

    assert group
    for row in group:
        assert row.entrees_per_order >= 4.0
        assert row.order_count >= 20
    assert min(row.entrees_per_order for row in group) > max(
        row.entrees_per_order for row in fixtures_for("regular")
    )


def test_every_archetype_is_ranked_best_first() -> None:
    """Rank one is the strongest exemplar by that archetype's own measure."""
    config = shipped_config()

    for spec in config.personas:
        rows = fixtures_for(spec.persona_id)
        assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
        scores = [facts()[row.demo_id].measure(spec.fixture.measure) for row in rows]
        assert scores == sorted(scores, reverse=not spec.fixture.ascending)


# --------------------------------------------------------------------------
# Acceptance criterion 3: the narrative is good enough to paste directly into
# an opening message.
# --------------------------------------------------------------------------


def test_every_narrative_is_fully_rendered() -> None:
    """No unfilled placeholder ever reaches a visitor."""
    for row in fixture_population().persona_fixtures:
        assert "{" not in row.narrative
        assert "}" not in row.narrative
        assert row.narrative.strip() == row.narrative
        assert row.narrative.endswith(".")
        assert len(row.narrative) > 40


def test_every_number_in_a_narrative_is_a_number_in_its_row() -> None:
    """The sentence is re-derivable from the columns beside it.

    This is what makes "good enough to paste into an opening message" a
    property rather than a matter of taste: a reviewer who doubts a narrative
    checks it against the row rather than against the generator. It is the same
    argument ``order_items.unit_price`` is carried for.

    The store's published name is removed before the numbers are read out of
    the sentence, because a restaurant may be called "NC Town 1 Mall" and the
    1 in it is the locator's, not this row's.
    """
    for row in fixture_population().persona_fixtures:
        derivable = {
            str(row.order_count),
            str(row.points_balance),
            str(row.distinct_baskets),
            str(row.distinct_stores),
            str(row.days_since_order),
            str(row.home_store),
            f"{row.usual_share * 100:.0f}",
            f"{row.entrees_per_order:.1f}",
            f"{row.lifetime_spend:,.0f}".replace(",", ""),
            str(row.first_order_at.year),
            str(row.last_order_at.year),
        }
        sentence = row.narrative.replace(row.home_store_name or "", "")
        for stated in re.findall(r"\d[\d,.]*\d|\d", sentence):
            assert stated.replace(",", "") in derivable, (
                f"{row.demo_id}'s narrative states {stated!r}, which is not "
                "derivable from its row"
            )


def test_a_narrative_names_only_food_the_catalogue_publishes() -> None:
    """The one place a narrative could invent a menu item, checked.

    Everything Cilantro says about food comes from what Chipotle publishes, and
    a narrative is something Cilantro says. The identifiers are already
    catalogue foreign keys; this asserts the *prose* built from them names the
    same things, spelled the way the catalogue spells them.

    Not every archetype's narrative mentions food — the Lapsed Customer's is
    about the points they left behind, which is what PRD requirement P3 needs
    of it. The assertion follows the template rather than assuming: a narrative
    that names food names published food, and one that does not is left alone.
    """
    catalog = fixture_catalog()
    items = {item.item_id: item.name for item in catalog.menu_items}
    modifiers = {row.modifier_id: row.name for row in catalog.modifiers}
    named = 0

    for row in fixture_population().persona_fixtures:
        template = shipped_config().persona(row.persona_id).fixture.narrative
        if "{usual_order}" not in template and "{usual_item_name}" not in template:
            continue
        named += 1
        assert row.usual_item_id is not None
        assert items[row.usual_item_id].lower() in row.narrative.lower()
        for modifier_id in row.usual_modifiers:
            assert modifiers[modifier_id].lower() in row.narrative.lower()

    assert named, "no narrative names any food at all; the check is vacuous"


def test_the_personas_that_are_about_food_say_what_the_food_is() -> None:
    """The Regular and the Explorer are both defined by their usual.

    In opposite directions — the Regular has one and the Explorer does not —
    but both narratives have to name it, because "your usual is X" and "the
    nearest thing to a usual is X, and it is only 4% of your orders" are the
    two sentences those fixtures exist to make sayable.
    """
    for persona_id in ("regular", "explorer"):
        template = shipped_config().persona(persona_id).fixture.narrative
        assert "{usual_order}" in template or "{usual_item_name}" in template


def test_a_narrative_names_the_store_the_catalogue_publishes() -> None:
    """A narrative names a real restaurant, spelled as the locator spells it.

    ``home_store_name`` may be ``None`` — the locator publishes a name for most
    stores and not all — and a narrative that mentions the store at all must
    then fall back to identifying it rather than inventing one.
    """
    published = {store.store_id: store.name for store in stores()}

    for row in fixture_population().persona_fixtures:
        assert row.home_store_name == published[row.home_store]
        if "{store}" not in shipped_config().persona(row.persona_id).fixture.narrative:
            continue
        expected = row.home_store_name or f"store {row.home_store}"
        assert expected in row.narrative


def test_a_narrative_carries_no_name_so_it_cannot_go_stale() -> None:
    """``display_name`` is editable, so a narrative must not embed one.

    RFC-001 section 04 lets a visitor change their display name. A narrative
    with a name baked into it is a sentence that is wrong the moment they do.
    The entry flow of issue #67 joins the live name to this sentence instead —
    which is also how the ticket itself writes the example, nameless.
    """
    population = fixture_population()
    narratives = " ".join(row.narrative for row in population.persona_fixtures)

    for visitor in population.demo_visitors:
        assert visitor.display_name not in narratives


def test_each_archetype_reads_as_a_different_kind_of_person() -> None:
    """Seven archetypes, seven distinguishable openings.

    A demo that switches personas and shows the same sentence with different
    numbers in it has not demonstrated personas.
    """
    openings = {
        row.persona_id: row.narrative.split(",")[0]
        for row in fixture_population().persona_fixtures
        if row.rank == 1
    }

    assert len(openings) == len(shipped_config().personas)
    assert len(set(openings.values())) == len(openings)


def test_the_three_prd_personas_read_the_way_the_prd_describes_them() -> None:
    """The narrative says the thing the persona is for.

    Weak as prose criticism and strong as a regression test: a narrative
    rewritten to stop mentioning the Lapsed Customer's points has stopped being
    an opening message for the persona PRD requirement P3 is about.
    """
    lapsed = fixtures_for("lapsed")[0]
    regular = fixtures_for("regular")[0]
    explorer = fixtures_for("explorer")[0]

    assert f"{lapsed.points_balance:,}" in lapsed.narrative
    assert "unredeemed" in lapsed.narrative
    assert str(regular.order_count) in regular.narrative
    assert f"{regular.usual_share:.0%}" in regular.narrative
    assert str(explorer.distinct_baskets) in explorer.narrative
    assert "usual" in explorer.narrative


# --------------------------------------------------------------------------
# The properties selection itself has to keep.
# --------------------------------------------------------------------------


def test_no_editable_column_can_change_who_the_fixtures_are() -> None:
    """The containment property of RFC-001 section 04, applied to fixtures.

    ``display_name``, ``home_store_override`` and ``stated_preferences`` are the
    three columns a visitor may edit. Selection reads none of them, so rewriting
    all three for every customer must leave the fixtures identical. Asserted by
    doing exactly that rather than by reading the code, because the property is
    what matters and a future refactor is what would break it.
    """
    population = fixture_population()
    meddled = dataclasses.replace(
        population,
        demo_visitors=tuple(
            dataclasses.replace(
                visitor,
                display_name=f"Edited {visitor.demo_id}",
                home_store_override=99999,
                stated_preferences="no cilantro, extra everything",
            )
            for visitor in population.demo_visitors
        ),
    )

    again = select_fixtures(
        tuple(measured(meddled).values()),
        shipped_config(),
        fixture_catalog(),
        stores(),
        fixture_terms(),
    )

    assert again == population.persona_fixtures


def test_selection_is_a_pure_function_of_the_population() -> None:
    """Same population, same fixtures. The population's digest depends on it."""
    rows = tuple(facts().values())

    first = select_fixtures(
        rows, shipped_config(), fixture_catalog(), stores(), fixture_terms()
    )
    second = select_fixtures(
        rows, shipped_config(), fixture_catalog(), stores(), fixture_terms()
    )

    assert first == second == fixture_population().persona_fixtures


def test_a_smaller_population_supplies_qualifying_fixtures_or_none() -> None:
    """Selection never pads to make a count come out right.

    Checked against the criteria rather than against a count. A count is the
    obvious assertion and the wrong one: whether sixty customers happen to
    yield fewer exemplars than five hundred depends on where the bars sit, and
    it moved when issue #27 changed the earn rate. What must never move is
    that everybody on the roster earned their place — the failure this guards
    is a demo assigning a visitor "the Regular" who turns out to have no usual,
    because a quota had to be filled.

    The exclusion is asserted too. A bound nobody fails is a bound that proves
    nothing, so this also insists that the thin population really did contain
    customers the criteria turned away.
    """
    small = small_population()
    config = small_config()
    rows = measured(small)
    terms = fixture_terms()

    counts = Counter(row.persona_id for row in small.persona_fixtures)
    for spec in config.personas:
        assert counts[spec.persona_id] <= config.fixtures_per_persona
    for row in small.persona_fixtures:
        assert rows[row.demo_id].satisfies(config.persona(row.persona_id), terms)

    chosen = {row.demo_id for row in small.persona_fixtures}
    assert [
        row
        for row in rows.values()
        if row.demo_id not in chosen
        and not row.satisfies(config.persona(row.persona_id), terms)
    ]


def test_a_customer_who_fails_one_bound_is_not_a_fixture() -> None:
    """Every bound, not most of them. Raising one bar empties that roster."""
    config = shipped_config()
    spec = config.persona("regular")
    tightened = dataclasses.replace(
        config,
        personas=tuple(
            dataclasses.replace(
                row,
                fixture=dataclasses.replace(
                    row.fixture, at_least=(("usual_share", 1.01),)
                ),
            )
            if row.persona_id == "regular"
            else row
            for row in config.personas
        ),
    )

    chosen = select_fixtures(
        tuple(facts().values()), tightened, fixture_catalog(), stores(), fixture_terms()
    )

    assert spec.fixture.at_least
    assert not [row for row in chosen if row.persona_id == "regular"]
    assert [row for row in chosen if row.persona_id == "explorer"]


def test_a_customer_with_no_orders_is_not_measured() -> None:
    """There is nothing to say about them, and a zero would rank them first.

    ``usual_share`` of zero is the Explorer's ranking key read at its best
    value, so admitting an empty history would put a customer who has never
    ordered at the top of the Explorer roster.
    """
    population = fixture_population()
    silent = dataclasses.replace(
        population,
        orders=tuple(
            order for order in population.orders if order.demo_id != "demo-0001"
        ),
    )

    rows = measured(silent)
    chosen = select_fixtures(
        tuple(rows.values()),
        shipped_config(),
        fixture_catalog(),
        stores(),
        fixture_terms(),
    )

    assert "demo-0001" in facts()
    assert "demo-0001" not in rows
    assert not [row for row in chosen if row.demo_id == "demo-0001"]


def test_the_fixtures_land_with_the_rest_of_the_population() -> None:
    """``persona_fixtures`` is a table like any other, and is written like one."""
    population = fixture_population()

    assert "persona_fixtures" in dict(population.tables())
    assert population.table("persona_fixtures") == population.persona_fixtures
    assert population.manifest()["tables"]


# --------------------------------------------------------------------------
# The config contract.
# --------------------------------------------------------------------------


def test_every_measure_the_config_offers_is_a_fact_that_is_measured() -> None:
    """The vocabulary and the record agree, or a criterion silently never bites."""
    fields = {field.name for field in dataclasses.fields(CustomerFacts)}
    one = next(iter(facts().values()))

    assert set(MEASURES) <= fields
    for name in MEASURES:
        assert isinstance(one.measure(name), float)


def test_every_shipped_criterion_names_a_real_measure() -> None:
    """Including ``rank_by``, whose direction prefix is not part of the name."""
    for spec in shipped_config().personas:
        assert spec.fixture.measure in MEASURES
        for name, _ in (*spec.fixture.at_least, *spec.fixture.at_most):
            assert name in MEASURES


def test_a_criterion_naming_nothing_is_refused(tmp_path: Path) -> None:
    """A misspelt bound would be a bound that excludes nobody.

    Which is the worst way for this to fail: the archetype keeps filling its
    roster, from customers who were never checked.
    """
    path = tmp_path / "typo.toml"
    path.write_text(
        PACKAGED.read_text(encoding="utf-8").replace(
            "usual_share = 0.85", "usual_shrae = 0.85"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="not a measured fact"):
        load_config(path)


def test_bounds_that_no_customer_could_clear_are_refused(tmp_path: Path) -> None:
    """An archetype that can contribute nothing is a config mistake, not a result."""
    path = tmp_path / "impossible.toml"
    path.write_text(
        PACKAGED.read_text(encoding="utf-8").replace(
            "[personas.fixture.at_most]\nusual_share = 0.15",
            "[personas.fixture.at_most]\nusual_share = 0.15\ndistinct_stores = 2",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="no customer can clear both"):
        load_config(path)


def test_a_narrative_naming_a_field_that_does_not_exist_is_refused() -> None:
    """Refused rather than rendered: a visitor must never be shown a hole."""
    config = shipped_config()
    broken = dataclasses.replace(
        config,
        personas=tuple(
            dataclasses.replace(
                row, fixture=dataclasses.replace(row.fixture, narrative="hi {nonsense}")
            )
            if row.persona_id == "regular"
            else row
            for row in config.personas
        ),
    )

    with pytest.raises(ConfigError, match="does not render"):
        select_fixtures(
            tuple(facts().values()),
            broken,
            fixture_catalog(),
            stores(),
            fixture_terms(),
        )


def test_the_shipped_criteria_are_the_ones_the_assertions_check() -> None:
    """The bars in ``population.toml`` and the bars in this file agree.

    Both are stated, deliberately. The config's bar decides who qualifies; this
    file's bar is what the PRD was promised. If someone loosens the config to
    fill a roster, this is what notices.
    """
    config, terms = shipped_config(), fixture_terms()

    def bar(bounds: tuple[tuple[str, Bound], ...], name: str) -> float:
        return resolve(dict(bounds)[name], terms)

    assert bar(config.persona("regular").fixture.at_least, "usual_share") >= 0.85
    assert bar(config.persona("explorer").fixture.at_most, "usual_share") <= 0.15
    lapsed = config.persona("lapsed").fixture.at_least
    assert bar(lapsed, "days_since_order") >= 90
    # An identity, not an inequality, and the strongest of the three. The
    # Lapsed Customer's bar is not a number this file and the config are asked
    # to keep in step -- it is a name, read off the published terms at
    # selection time. That is the whole repair: this criterion first shipped as
    # the literal 1250, a copy of a config key issue #27 then deleted, and the
    # copy survived the thing it copied.
    assert dict(lapsed)["points_balance"] == "costliest_reward"
    for persona_id in PRD_PERSONAS:
        assert config.persona(persona_id).fixture.narrative


def test_an_archetype_with_no_criteria_is_refused(tmp_path: Path) -> None:
    """A new archetype without fixture criteria would be invisible to the demo.

    Silently: it would generate customers, they would behave as configured, and
    no visitor would ever be assigned one. Refusing at load is the only point at
    which that is noticeable. The last archetype in the shipped file has its
    whole ``[personas.fixture]`` section cut away here, which is what adding an
    eighth archetype and forgetting one looks like.
    """
    path = tmp_path / "criteria-less.toml"
    text = PACKAGED.read_text(encoding="utf-8")
    last = text.rindex("[personas.fixture]")
    path.write_text(text[:last], encoding="utf-8")

    with pytest.raises(ConfigError, match="needs fixture criteria"):
        load_config(path)


# --------------------------------------------------------------------------
# A bound that is a published fact rather than a chosen number
# --------------------------------------------------------------------------


def test_every_published_bound_has_a_reader() -> None:
    """The config's vocabulary and this module's resolvers are one list.

    The same correspondence :data:`MEASURES` has with ``CustomerFacts``, for
    the same reason: a name the config accepts and nothing can resolve is a
    criterion that raises at generation time, and a resolver for a name the
    config refuses is dead code that reads as coverage.
    """
    assert set(PUBLISHED_READERS) == set(PUBLISHED_BOUNDS)


def test_a_published_bound_is_read_and_not_stored() -> None:
    """The bar moves when Chipotle's does, which is what 1250 could not do.

    The defect this replaces was not a wrong number, it was a *copied* one: the
    Lapsed Customer's bar was written as the literal value of a config key, so
    when issue #27 deleted the key the criterion went on comparing against a
    number nothing published any more. Here the published Rewards Exchange is
    repriced out of every lapsed customer's reach, and the roster has to empty.
    A bar stored anywhere in this package would leave it standing.

    Every other archetype is bounded on facts about orders, so the same change
    must leave those rosters identical — which is what says the emptying is the
    criterion biting rather than the terms perturbing the whole selection.
    """
    terms = fixture_terms()
    richest = max(row.points_balance for row in facts().values())
    unreachable = dataclasses.replace(
        terms,
        rewards=tuple(
            dataclasses.replace(reward, point_cost=reward.point_cost + richest)
            for reward in terms.rewards
        ),
    )
    rows = tuple(facts().values())

    shipped = select_fixtures(rows, shipped_config(), fixture_catalog(), stores(), terms)
    repriced = select_fixtures(
        rows, shipped_config(), fixture_catalog(), stores(), unreachable
    )

    assert [row for row in shipped if row.persona_id == "lapsed"]
    assert not [row for row in repriced if row.persona_id == "lapsed"]
    assert [row for row in shipped if row.persona_id != "lapsed"] == [
        row for row in repriced if row.persona_id != "lapsed"
    ]


def test_a_bound_that_names_nothing_published_is_refused(tmp_path: Path) -> None:
    """A string bound is a name or it is a mistake; there is no third reading.

    Refused at load rather than at selection, for the reason the measure
    vocabulary is: a criterion nothing can resolve would otherwise surface as a
    crash halfway through generating five hundred customers, or -- worse, if
    anyone ever made it lenient -- as an archetype that quietly admits
    everybody.
    """
    path = tmp_path / "prose-bound.toml"
    text = PACKAGED.read_text(encoding="utf-8")
    path.write_text(
        text.replace('points_balance = "costliest_reward"', 'points_balance = "lots"'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="neither a number nor a published"):
        load_config(path)
