"""The gold declarations, checked against everything they have to agree with.

`chip_chat.databricks.gold` is four SQL statements, a metric and a table of
thresholds that a pipeline consumes on a cluster nobody runs in CI. So the
assertions here come in three kinds.

The first is `test_silver.py`'s: the declaration says X, and something else in
this repository — RFC-001 §04's own schema, silver's table list, the
generator's config, the Unity Catalog layout, the Terraform, the notebooks —
independently says X too.

The second is the one this layer adds. The queries are held to the properties
that make a mart trustworthy without running them: exactly one wall clock per
mart, no tie broken on arrival order, no threshold written twice, and nothing
anywhere selecting from `demo_visitors`.

The third is the confidence metric, which is an *algorithm* and is therefore
run. `test_a_regular_is_stated_and_an_explorer_is_not` is issue #36's third
acceptance criterion, made against the bounds `population.toml` actually admits
a Regular and an Explorer on — so a retune of the generator that broke the
calibration fails `make ci` rather than surfacing as a wrong answer in a demo.

The two things these cannot check are the live marts and Spark's own reading of
the SQL, and that is what `databricks/notebooks/gold_verify.py` is for.
"""

import math
import re
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from chip_chat.data_gen import load_config
from chip_chat.databricks import catalog, gold, silver

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "databricks" / "notebooks" / "gold_marts.py"
VERIFY = REPO / "databricks" / "notebooks" / "gold_verify.py"
TERRAFORM = REPO / "infra" / "terraform" / "databricks_gold.tf"

CONFIG = load_config()


def resolve(stream: str, table: str) -> str:
    """Qualify a silver table the way the notebook does.

    The stream is a plain string here, as it is everywhere in `gold.py`, which
    may not import `catalog` to get the literal type. The cast is the seam, and
    `test_the_streams_are_the_ones_unity_catalog_has` is what makes it safe.
    """
    return catalog.table(gold.SOURCE_LAYER, cast("catalog.Stream", stream), table)


def rendered(candidate: gold.Mart) -> str:
    """Return one mart's SQL, filled."""
    return gold.query(candidate, resolve)


def statements(candidate: gold.Mart) -> str:
    """Return one mart's SQL with its comments removed.

    The comments in these queries argue with the reader — several of them name
    the thing the query deliberately does *not* do — so a check that reads the
    text has to read the statements rather than the prose around them.
    """
    return "\n".join(line.split("--", 1)[0] for line in rendered(candidate).splitlines())


def code(notebook: str) -> str:
    """Return a notebook with its markdown cells removed, for the same reason."""
    return "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )


# --- Agreement with the Unity Catalog layout --------------------------------


def test_the_streams_are_the_ones_unity_catalog_has() -> None:
    """`gold.STREAMS` is a copy, because gold.py may not import a sibling."""
    assert gold.STREAMS == catalog.STREAMS


def test_gold_is_a_layer_of_the_medallion() -> None:
    assert gold.LAYER in catalog.LAYERS


def test_gold_reads_silver_and_never_bronze() -> None:
    """A mart computed from what arrived rather than from what is true would
    quietly disagree with the layer a human reads."""
    assert gold.SOURCE_LAYER == silver.LAYER
    for candidate in gold.MARTS:
        assert "bronze" not in rendered(candidate)


@pytest.mark.parametrize("stream", catalog.STREAMS)
def test_the_schema_name_is_the_one_terraform_created(stream: catalog.Stream) -> None:
    assert gold.schema_name(stream) == catalog.schema("gold", stream).name


def test_an_unknown_stream_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        gold.schema_name("real")


def test_every_mart_is_published_into_a_schema_that_exists() -> None:
    for candidate in gold.MARTS:
        assert candidate.stream in gold.STREAMS
        stream = cast("catalog.Stream", candidate.stream)
        assert catalog.schema(gold.LAYER, stream).name == gold.schema_name(stream)


# --- Agreement with RFC-001 §04 ---------------------------------------------


def test_the_four_marts_are_the_four_the_rfc_names() -> None:
    assert {candidate.name for candidate in gold.MARTS} == set(gold.RFC_COLUMNS)
    assert len(gold.MARTS) == 4


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_the_schema_is_the_rfcs_exactly(candidate: gold.Mart) -> None:
    """The issue's brief: the schemas are fixed and must be matched exactly,
    because the agent's read tools query these columns by name.

    `derived_at` is the one addition, and only where §04 did not already name
    it: RFC-001 §10 requires every mart to be servable stale with its own
    timestamp, and a mart with nowhere to put one cannot be.
    """
    published = gold.RFC_COLUMNS[candidate.name]
    expected = published
    if gold.DERIVED_AT not in published:
        expected = (*published, gold.DERIVED_AT)
    assert gold.column_names(candidate) == expected


def test_the_only_column_the_rfc_does_not_name_is_the_timestamp() -> None:
    """Stated as its own assertion so that adding a fifth column to a mart is
    a decision somebody makes on purpose rather than a diff nobody reads."""
    for candidate in gold.MARTS:
        extra = set(gold.column_names(candidate)) - set(gold.RFC_COLUMNS[candidate.name])
        assert extra <= {gold.DERIVED_AT}


def test_every_visitor_scoped_mart_carries_the_column_the_policy_compares() -> None:
    """Issue #43 protects these with row access policies, and a policy needs a
    column to compare. `item_affinity` is the one mart that is a fact about the
    population rather than about a visitor, and it carries no demo_id at all —
    which is the honest way to say a table needs no policy."""
    for candidate in gold.MARTS:
        has_demo_id = "demo_id" in gold.column_names(candidate)
        assert has_demo_id == (candidate.name != "item_affinity")


def test_derived_at_is_required_on_every_mart() -> None:
    """#36's fourth acceptance criterion. A required column becomes a fatal
    expectation, which is what makes "populated on every row" enforced rather
    than intended."""
    for candidate in gold.MARTS:
        assert gold.DERIVED_AT in candidate.required
        names = [check.name for check in gold.expectations(candidate)]
        assert f"{gold.DERIVED_AT}_is_present" in names


# --- Agreement with silver ---------------------------------------------------


def test_every_source_is_a_table_silver_actually_conforms() -> None:
    for source in gold.sources():
        conformed = silver.table(source.table)
        assert conformed.stream == source.stream


def test_no_mart_reads_the_table_holding_the_editable_fields() -> None:
    """RFC-001 §04 answers PRD Q2 by containment: the three fields a visitor
    may edit are columns of `demo_visitors`, no editable field is an input to a
    mart, and so no edit can invalidate one. The RFC says a reviewer checks the
    property by confirming nothing under the medallion pipeline selects from
    that table. This is that reviewer."""
    for source in gold.sources():
        assert source.table not in gold.FORBIDDEN_SOURCES
    for candidate in gold.MARTS:
        for forbidden in gold.FORBIDDEN_SOURCES:
            assert forbidden not in rendered(candidate)


def test_the_forbidden_table_is_one_that_exists() -> None:
    """A guard against a table nothing has heard of guards nothing. If
    `demo_visitors` is ever renamed, this fails here rather than leaving the
    containment check silently satisfied by a typo."""
    for forbidden in gold.FORBIDDEN_SOURCES:
        assert silver.table(forbidden).stream == "synthetic"


def test_the_three_editable_fields_are_all_on_the_forbidden_table() -> None:
    """The containment argument is only worth making if the editable fields
    really do all live behind the one table this layer refuses to read."""
    visitors = silver.table("demo_visitors")
    assert visitors.name in gold.FORBIDDEN_SOURCES
    for editable in ("display_name", "home_store_override", "stated_preferences"):
        for candidate in gold.MARTS:
            assert editable not in rendered(candidate)


def test_no_mart_reads_the_ledger() -> None:
    """None of RFC-001 §04's four marts carries a points balance. Stored value
    reaches a visitor through `persona_fixtures.points_balance` and issue #27's
    reconciliation, and a mart that read the ledger without publishing it would
    be a join nobody can point at a column for."""
    for candidate in gold.MARTS:
        assert "loyalty_ledger" not in rendered(candidate)


def test_money_survives_a_sum_that_silvers_type_would_not_hold() -> None:
    """Same scale, more digits: these are sums of eighteen months of orders
    rather than the price of one thing, and a sum that overflows its decimal in
    Spark is a null rather than an error."""
    gold_precision, gold_scale = _decimal(gold.MONEY)
    silver_precision, silver_scale = _decimal(silver.MONEY)
    assert gold_scale == silver_scale
    assert gold_precision > silver_precision


def _decimal(sql_type: str) -> tuple[int, int]:
    """Return the precision and scale of a DECIMAL type."""
    match = re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", sql_type)
    assert match, f"{sql_type} is not a decimal"
    return int(match.group(1)), int(match.group(2))


# --- Agreement with the generator -------------------------------------------


def test_the_settled_statuses_are_the_generators_own() -> None:
    """The set that earns loyalty points is the set a mart counts. A copy,
    because gold.py may not import a sibling; asserted equal here so that a
    retune in one place cannot silently disagree with the other."""
    assert CONFIG.orders.settled_statuses == gold.SETTLED_STATUSES


def test_a_status_a_mart_counts_is_a_status_an_order_can_reach() -> None:
    assert set(gold.SETTLED_STATUSES) <= set(CONFIG.orders.statuses)


def test_the_statuses_a_mart_ignores_are_the_ones_that_did_not_happen() -> None:
    """Named rather than left as "everything else": a cancelled order never
    happened and a refunded one had its money returned, and if the generator
    grows a fourth status somebody has to decide which of the two it is."""
    ignored = set(CONFIG.orders.statuses) - set(gold.SETTLED_STATUSES)
    assert ignored == {"CANCELLED", "REFUNDED"}


def test_the_entree_category_is_the_one_the_generator_orders_from() -> None:
    assert CONFIG.catalogue.entree_category == gold.ENTREE_CATEGORY


def test_a_lapse_is_the_same_length_of_silence_the_population_was_built_with() -> None:
    """Ninety days is a product decision, and it is made once. A mart that
    called somebody lapsed at sixty would disagree with the archetype the
    population put them in."""
    assert CONFIG.texture.lapsed_after_days == gold.LAPSED_AFTER_DAYS


def test_every_calibrated_archetype_is_one_the_population_contains() -> None:
    known = {spec.persona_id for spec in CONFIG.personas}
    for calibration in gold.CALIBRATION:
        assert calibration.persona_id in known


# --- The confidence metric ---------------------------------------------------


def test_a_confidence_is_never_above_the_share_it_is_computed_from() -> None:
    """The defining property: it is a lower bound. Everything the calibration
    argument claims about the Explorer rests on this holding at every sample
    size, so it is asserted over a grid rather than argued."""
    for orders in (1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 500, 5000):
        for repeats in {0, 1, orders // 4, orders // 2, orders - 1, orders}:
            if not 0 <= repeats <= orders:
                continue
            share = Decimal(repeats) / Decimal(orders)
            assert gold.confidence(repeats, orders) <= share


def test_more_evidence_for_the_same_share_is_more_confidence() -> None:
    """Two orders out of three is a share of 67% and evidence of nothing. A
    metric that cannot tell that apart from forty out of sixty is one that will
    state an Explorer's accident as their habit."""
    previous = Decimal(-1)
    for orders in (4, 8, 20, 40, 100, 400, 4000):
        value = gold.confidence(orders // 2, orders)
        assert value > previous
        previous = value


def test_a_confidence_is_a_proportion() -> None:
    for orders in (1, 7, 50, 1000):
        for repeats in range(orders + 1):
            value = gold.confidence(repeats, orders)
            assert Decimal(0) <= value <= Decimal(1)
            if repeats % 97 == 0:
                assert gold.band(value)


def test_certainty_is_never_claimed_from_one_order() -> None:
    """One order is not a habit, and a raw share would call it a certainty."""
    assert gold.confidence(1, 1) < gold.HEDGED.floor


def test_a_confidence_needs_orders_to_be_computed_from() -> None:
    with pytest.raises(ValueError, match="at least one order"):
        gold.confidence(0, 0)


def test_a_confidence_refuses_a_count_that_is_not_a_proportion() -> None:
    with pytest.raises(ValueError, match="not a proportion"):
        gold.confidence(5, 4)
    with pytest.raises(ValueError, match="not a proportion"):
        gold.confidence(-1, 4)


def test_the_sql_and_the_python_compute_the_same_number() -> None:
    """A metric two implementations disagree about is a metric nobody can
    check, and only one of the two runs in CI.

    So the SQL is translated to Python arithmetic here — `GREATEST` is `max`,
    `LEAST` is `min`, and the rest is already Python — and run against the
    reference. This is not Spark and does not pretend to be: what it proves is
    that the two expressions are the same arithmetic, which is the half of the
    agreement that can drift silently. Spark's own reading of the SQL is
    `gold_verify.py`'s job.
    """
    expression = gold.confidence_expression("repeats", "order_count")
    body = expression.removeprefix("CAST(").removesuffix(f" AS {gold.CONFIDENCE})")
    translated = body.replace("GREATEST", "max").replace("LEAST", "min")
    for orders in (1, 2, 3, 7, 30, 250, 3000):
        for repeats in (0, 1, orders // 3, orders // 2, orders):
            if repeats > orders:
                continue
            # The string is built above, out of this module's own constants.
            evaluated = eval(
                translated,
                {"max": max, "min": min, "sqrt": math.sqrt},
                {"repeats": repeats, "order_count": orders},
            )
            assert gold.confidence(repeats, orders) == Decimal(repr(evaluated)).quantize(
                Decimal("0.0001")
            )


# --- The calibration #36 is graded on ----------------------------------------


def _fixture_bound(persona_id: str, measure: str, *, upper: bool) -> float:
    """Return the bound `population.toml` admits a fixture of `persona_id` on."""
    for spec in CONFIG.personas:
        if spec.persona_id != persona_id:
            continue
        bounds = spec.fixture.at_most if upper else spec.fixture.at_least
        for name, value in bounds:
            if name == measure:
                assert isinstance(value, float | int), (
                    f"{persona_id}.{measure} is read off published terms, and "
                    "this calibration needs a number"
                )
                return float(value)
    raise AssertionError(f"{persona_id} has no bound on {measure}")


def test_a_regular_is_stated_and_an_explorer_is_not() -> None:
    """Issue #36's third acceptance criterion, against the population's own
    admission bounds rather than against numbers chosen to pass.

    A customer is only a Regular fixture above an 85% usual share over at least
    thirty orders, and only an Explorer fixture at or below a 15% share. The
    claim is that the bands separate those two at *every* sample size the
    bounds allow — the Regular at their worst case, and the Explorer however
    long their history runs.
    """
    floor_share = _fixture_bound("regular", "usual_share", upper=False)
    floor_orders = int(_fixture_bound("regular", "order_count", upper=False))
    worst_case = gold.confidence(math.ceil(floor_share * floor_orders), floor_orders)
    assert gold.band(worst_case) is gold.STATED

    ceiling_share = _fixture_bound("explorer", "usual_share", upper=True)
    explorer_orders = int(_fixture_bound("explorer", "order_count", upper=False))
    for orders in (explorer_orders, 50, 200, 2_000, 50_000):
        if orders < explorer_orders:
            continue
        best_case = gold.confidence(math.floor(ceiling_share * orders), orders)
        assert gold.band(best_case) is gold.NO_USUAL


def test_the_calibrated_archetypes_land_where_the_declaration_says() -> None:
    """`gold.CALIBRATION` is what `gold_verify.py` asserts against the live
    marts, so it has to be true of the arithmetic before it is asserted of the
    data."""
    expected = {row.persona_id: row.expected for row in gold.CALIBRATION}
    assert expected["regular"] is gold.STATED
    assert expected["explorer"] is gold.NO_USUAL


def test_the_boundary_is_documented_in_words() -> None:
    """The criterion asks for the boundary to be documented, and a band with no
    sentence in it documents nothing."""
    for reading in gold.CONFIDENCE_BANDS:
        assert reading.meaning.strip()
        assert reading.licence.strip()
        assert reading.name.islower()


def test_the_bands_partition_the_range() -> None:
    floors = [reading.floor for reading in gold.CONFIDENCE_BANDS]
    assert floors == sorted(floors, reverse=True)
    assert len(set(floors)) == len(floors)
    assert floors[-1] == Decimal(0)


def test_a_value_outside_the_range_is_not_a_confidence() -> None:
    with pytest.raises(ValueError, match=r"in \[0, 1\]"):
        gold.band(Decimal("1.5"))
    with pytest.raises(ValueError, match=r"in \[0, 1\]"):
        gold.band(Decimal("-0.1"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.0", "stated"),
        ("0.60", "stated"),
        ("0.5999", "hedged"),
        ("0.25", "hedged"),
        ("0.2499", "no_usual"),
        ("0", "no_usual"),
    ],
)
def test_a_band_is_read_at_its_own_edge(value: str, expected: str) -> None:
    assert gold.band(Decimal(value)).name == expected


# --- The queries -------------------------------------------------------------


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_every_placeholder_is_filled(candidate: gold.Mart) -> None:
    """A leftover brace is a query that fails on the cluster, minutes into an
    update, rather than here."""
    sql = rendered(candidate)
    assert "{" not in sql
    assert "}" not in sql


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_every_declared_source_is_read_and_every_table_read_is_declared(
    candidate: gold.Mart,
) -> None:
    sql = rendered(candidate)
    for source in candidate.sources:
        assert f"{{{source.alias}}}" in candidate.template
        assert resolve(source.stream, source.table) in sql
    for other in silver.TABLES:
        if other.name in {source.table for source in candidate.sources}:
            continue
        assert resolve(other.stream, other.name) not in sql


def test_a_query_that_names_a_source_it_did_not_declare_is_refused() -> None:
    undeclared = gold.Mart(
        name="invented",
        stream=gold.STREAM,
        grain="one row per mistake",
        columns=(gold.Column(name="demo_id", sql_type="STRING", why="whose"),),
        sources=(),
        template="SELECT demo_id FROM {demo_visitors}",
        comment="never declared",
    )
    with pytest.raises(ValueError, match="declares no source"):
        gold.query(undeclared, resolve)


def test_a_source_may_not_be_named_after_a_threshold() -> None:
    """`{money}` is a type and `{settled}` is a status list. A source that
    claimed one of those names would silently shadow it."""
    shadowing = gold.Mart(
        name="invented",
        stream=gold.STREAM,
        grain="one row per mistake",
        columns=(gold.Column(name="demo_id", sql_type="STRING", why="whose"),),
        sources=(
            gold.Source(alias="money", stream="synthetic", table="orders", why="no"),
        ),
        template="SELECT demo_id FROM {money}",
        comment="never declared",
    )
    with pytest.raises(ValueError, match="already a threshold placeholder"):
        gold.query(shadowing, resolve)


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_a_mart_reads_the_wall_clock_exactly_once(candidate: gold.Mart) -> None:
    """`derived_at` is the only wall-clock value in this layer. A second
    `current_timestamp()` would be a number that changes for no reason a
    visitor could be told, and criterion five would be false by construction."""
    assert statements(candidate).count("current_timestamp()") == 1


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_nothing_is_measured_against_the_wall_clock(candidate: gold.Mart) -> None:
    """`lapsed_flag` and `cadence_days` need a "now", and it is read out of the
    data — the latest settled order — for the reason the generator measures
    `days_since_order` against its window's fixed end. A customer who lapses
    further every day is not a reproducible measurement."""
    sql = statements(candidate)
    assert "now()" not in sql
    assert "current_date" not in sql
    if candidate.name == "customer_360":
        assert gold.AS_OF in sql


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_a_tie_is_never_broken_on_arrival_order(candidate: gold.Mart) -> None:
    """`first()` over an unordered group is reproducible right up until the
    files underneath it are rewritten, and the symptom is a customer's usual
    order changing for no reason a month later. Every pick here is a `max()`
    over a struct whose lower fields are the identity of the thing chosen."""
    sql = statements(candidate)
    assert "first(" not in sql
    assert "any_value(" not in sql


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_only_settled_orders_are_counted(candidate: gold.Mart) -> None:
    """One rule, applied to all four, so that no two columns called
    `order_count` count different orders."""
    sql = rendered(candidate)
    for status in gold.SETTLED_STATUSES:
        assert f"'{status}'" in sql
    assert "status IN (" in sql


@pytest.mark.parametrize("candidate", gold.MARTS, ids=lambda mart: mart.name)
def test_every_declared_column_is_produced_by_the_query(candidate: gold.Mart) -> None:
    """Weak on its own — a column named in a comment would satisfy it — and
    worth having anyway, because the failure it catches is a rename in one of
    the two places. `gold_verify.py` compares against the published schema,
    which is the strong form and needs a cluster."""
    sql = rendered(candidate)
    for column in gold.column_names(candidate):
        assert f"AS {column}" in sql or f"{column}," in sql or f"    {column}\n" in sql


def test_every_threshold_reaches_the_sql_it_governs() -> None:
    """A constant nothing substitutes is a constant with a docstring and no
    effect, which is worse than no constant at all."""
    assert str(gold.LAPSED_AFTER_DAYS) in rendered(gold.mart("customer_360"))
    assert str(gold.MINIMUM_CO_ORDERS) in rendered(gold.mart("item_affinity"))
    assert gold.PERIOD_FORMAT in rendered(gold.mart("spend_summary"))
    usual = rendered(gold.mart("usual_order"))
    assert gold.ENTREE_CATEGORY in usual
    assert "sqrt(" in usual


def test_the_published_period_matches_the_format_that_produced_it() -> None:
    """The expectation is a regex and the format is a Spark pattern, so they
    can disagree without either being wrong on its own."""
    assert re.fullmatch(gold.PERIOD_PATTERN, "2026-08")
    assert not re.fullmatch(gold.PERIOD_PATTERN, "2026-8")
    assert not re.fullmatch(gold.PERIOD_PATTERN, "2026-08-01")
    assert len(gold.PERIOD_FORMAT.replace("-", "")) == 6


# --- Expectations ------------------------------------------------------------


def test_every_expectation_has_a_name_that_says_what_is_true() -> None:
    for candidate in gold.MARTS:
        for expectation in gold.expectations(candidate):
            assert expectation.name == expectation.name.lower()
            assert " " not in expectation.name
            assert expectation.why.strip()
            assert expectation.constraint.strip()


def test_expectation_names_are_unique_within_a_mart() -> None:
    for candidate in gold.MARTS:
        names = [item.name for item in gold.expectations(candidate)]
        assert len(names) == len(set(names))


def test_two_expectations_with_one_name_are_refused() -> None:
    """One of them would be silently unreported in the event log."""
    colliding = gold.Mart(
        name="invented",
        stream=gold.STREAM,
        grain="one row per mistake",
        columns=(gold.Column(name="demo_id", sql_type="STRING", why="whose"),),
        sources=(),
        template="SELECT 1",
        comment="never declared",
        required=("demo_id",),
        expectations=(
            gold.Expectation(
                name="demo_id_is_present", constraint="true", why="a collision"
            ),
        ),
    )
    with pytest.raises(ValueError, match="two expectations with one name"):
        gold.expectations(colliding)


def test_a_required_column_is_a_column_the_mart_declares() -> None:
    for candidate in gold.MARTS:
        assert set(candidate.required) <= set(gold.column_names(candidate))


def test_every_key_a_reader_joins_on_is_required() -> None:
    """A null in one of these is a row the serving layer cannot answer from,
    and it would arrive as an empty result rather than as an error."""
    for candidate in gold.MARTS:
        for column in gold.column_names(candidate):
            if column.endswith("_id") or column in {"period", "lift", "confidence"}:
                assert column in candidate.required


def test_a_constraint_only_names_columns_the_mart_has() -> None:
    """An expectation over a column that does not exist fails the update with
    an analysis error, which reads like the mart is broken rather than like the
    check is."""
    for candidate in gold.MARTS:
        columns = set(gold.column_names(candidate))
        for expectation in gold.expectations(candidate):
            for word in re.findall(r"[a-z_][a-z0-9_]*", expectation.constraint):
                if word in {"is", "not", "null", "and", "or", "rlike", "true", "false"}:
                    continue
                assert word in columns, (
                    f"{candidate.name}.{expectation.name} names {word!r}"
                )


# --- Lookups ------------------------------------------------------------------


def test_mart_lookup_refuses_a_name_nothing_publishes() -> None:
    with pytest.raises(KeyError, match="no gold mart"):
        gold.mart("customer_720")


def test_marts_for_refuses_an_unknown_stream() -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        list(gold.marts_for("imaginary"))


def test_every_mart_is_reachable_from_its_stream() -> None:
    reachable = [
        candidate.name for stream in gold.STREAMS for candidate in gold.marts_for(stream)
    ]
    assert sorted(reachable) == sorted(candidate.name for candidate in gold.MARTS)


def test_no_two_marts_share_a_name() -> None:
    names = [candidate.name for candidate in gold.MARTS]
    assert len(names) == len(set(names))


def test_the_sources_are_listed_once_each() -> None:
    listed = gold.sources()
    assert len(listed) == len({(item.stream, item.table) for item in listed})
    assert len(listed) == 3


def test_every_mart_says_what_one_row_is() -> None:
    for candidate in gold.MARTS:
        assert candidate.grain.strip()
        assert candidate.comment.strip()
        for column in candidate.columns:
            assert column.why.strip()


# --- The notebooks and the Terraform ----------------------------------------


@pytest.fixture(scope="module")
def notebook() -> str:
    assert NOTEBOOK.exists(), f"{NOTEBOOK} is missing"
    return NOTEBOOK.read_text()


@pytest.fixture(scope="module")
def verify() -> str:
    assert VERIFY.exists(), f"{VERIFY} is missing"
    return VERIFY.read_text()


@pytest.fixture(scope="module")
def terraform() -> str:
    assert TERRAFORM.exists(), f"{TERRAFORM} is missing"
    return TERRAFORM.read_text()


def test_the_pipeline_is_written_against_lakeflow_and_not_dlt(notebook: str) -> None:
    """Delta Live Tables became Lakeflow Spark Declarative Pipelines in 2026."""
    assert "from pyspark import pipelines as dp" in code(notebook)
    assert "import dlt" not in code(notebook)


def test_every_expectation_in_the_pipeline_is_fatal(notebook: str) -> None:
    """A mart is what the agent answers from, so a row that violates its own
    definition is not a line in an event log — it is a wrong answer in
    somebody's conversation."""
    assert "dp.expect_all_or_fail" in notebook
    assert "dp.expect_or_drop" not in notebook
    assert "dp.expect_all(" not in notebook


def test_the_pipeline_runs_the_query_the_module_holds(notebook: str) -> None:
    """The indirection is the point: `gold_verify.py` can re-run exactly what
    the pipeline ran, which is how criterion five becomes an assertion."""
    assert "gold.query(candidate, silver_name)" in notebook


def test_the_notebooks_never_name_the_table_holding_editable_fields(
    notebook: str, verify: str
) -> None:
    for source in (notebook, verify):
        for forbidden in gold.FORBIDDEN_SOURCES:
            assert forbidden not in code(source)


def test_the_pipeline_says_out_loud_what_it_refuses_to_read(notebook: str) -> None:
    """RFC-001 §04's containment is the mechanism behind its answer to PRD Q2,
    not an accident of which joins somebody happened to need. A reviewer opening
    the pipeline should find the argument there, and the check for it here."""
    for forbidden in gold.FORBIDDEN_SOURCES:
        assert forbidden in notebook


@pytest.mark.parametrize("path", [NOTEBOOK, VERIFY])
def test_a_markdown_cell_holds_no_code(path: Path) -> None:
    """Databricks reads a cell beginning `# MAGIC %md` as one markdown block:
    Python written below it in the same cell is rendered, not run. Nothing
    errors — the pipeline simply defines no tables and the update fails with
    `NO_TABLES_IN_PIPELINE`, which reads like the decorators are wrong."""
    for index, cell in enumerate(path.read_text().split("# COMMAND ----------")):
        lines = [line.rstrip() for line in cell.splitlines()]
        if not any(line.startswith("# MAGIC %md") for line in lines):
            continue
        code = [line for line in lines if line and not line.startswith(("#", "# MAGIC"))]
        assert not code, (
            f"{path.name} cell {index} is markdown and holds code: {code[:3]}"
        )


def test_the_verify_job_asserts_the_criteria_rather_than_reporting_them(
    verify: str,
) -> None:
    """A notebook that prints its findings and exits zero proves nothing."""
    assert "raise AssertionError" in verify
    assert "dbutils.notebook.exit" in verify


def test_the_verify_job_checks_the_demo_criterion_against_an_independent_answer(
    verify: str,
) -> None:
    """#36's second criterion. `persona_fixtures` was measured in Python by
    `chip_chat.data_gen.fixtures`, from the same history, with its own
    definition — and it has never seen the SQL."""
    assert "persona_fixtures" in verify
    assert "usual_item_id" in verify


def test_the_verify_job_rebuilds_the_marts_and_compares(verify: str) -> None:
    """#36's fifth criterion, and the reason the SQL lives in the module."""
    assert "gold.query(candidate, silver_name)" in verify
    assert "gold.DERIVED_AT" in verify


def test_the_verify_job_checks_the_calibration_the_bands_claim(verify: str) -> None:
    assert "gold.CALIBRATION" in verify
    assert "gold.CONFIDENCE_BANDS" in verify


def test_the_notebook_reads_the_configuration_terraform_supplies(
    notebook: str,
) -> None:
    for key in ("chip_chat.catalog", "chip_chat.lib_path"):
        assert f'spark.conf.get("{key}")' in notebook


def test_terraform_supplies_every_key_the_notebook_reads(
    terraform: str, notebook: str
) -> None:
    for key in ("chip_chat.catalog", "chip_chat.lib_path"):
        assert f'"{key}"' in terraform, f"{key} is read by the notebook and unset"
        assert f'spark.conf.get("{key}")' in notebook


def test_terraform_uploads_the_module_the_notebooks_import(terraform: str) -> None:
    """It is stdlib-only so that this upload is all the packaging needed."""
    assert "databricks/src/chip_chat/databricks/gold.py" in terraform
    assert "databricks/notebooks/gold_marts.py" in terraform
    assert "databricks/notebooks/gold_verify.py" in terraform


def test_the_pipeline_is_triggered_rather_than_continuous(terraform: str) -> None:
    """A continuous pipeline holds a cluster open, which is the cost trap #31
    exists to close and the most common way a month of credits disappears."""
    assert "continuous  = false" in terraform or "continuous = false" in terraform


def test_the_pipeline_runs_on_a_single_node(terraform: str) -> None:
    """The cost guardrail from #2's service inventory: single-node job
    clusters, never an always-on all-purpose one."""
    assert "num_workers  = 0" in terraform or "num_workers = 0" in terraform
    assert "SingleNode" in terraform


def test_gold_takes_no_checkpoint(terraform: str, notebook: str) -> None:
    """Materialized views recompute in full. Auto Loader's file ledger belongs
    to the layer that reads files, which is two layers down."""
    assert "chip_chat.checkpoint_uri" not in notebook
    assert "chip_chat.checkpoint_uri" not in terraform
