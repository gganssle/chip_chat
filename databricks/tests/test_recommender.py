"""The recommender, checked against everything it has to agree with.

`chip_chat.databricks.recommender` is a scoring rule, a promotion rule, a
sentence and three SQL templates that a job consumes on a cluster nobody runs in
CI. The assertions here come in four kinds.

The first is `test_gold.py`'s: the declaration says X, and something else in this
repository — `gold.py`'s own thresholds, silver's table list, the generator's
config, the Unity Catalog layout, the Terraform, the notebooks — independently
says X too. The support floor and the settled statuses matter most, because the
training run compares its own refit to the published `item_affinity` mart and a
difference in either would surface as a model disagreement rather than as the
bookkeeping difference it is.

The second is the scoring rule, which is an *algorithm* and is therefore run.
`test_a_popularity_list_wins_on_hits_and_loses_on_novelty` is the one to read:
it is PRD requirement P2 — *grounded in the visitor's actual ordering behaviour
rather than generic popularity, even if the popularity list scores well* — built
as a fixture and measured by the same function the training run uses.

The third is the properties that make the output trustworthy without running it:
nothing tried is ever recommended, ranks are contiguous, ties break on identity
rather than on arrival order, every threshold reaches the SQL that applies it,
and nothing anywhere names `demo_visitors`.

The fourth is `recommender_model.py`, which cannot be imported here — MLflow is
deliberately not in this lockfile — and is therefore read as text, the way this
suite already reads the notebooks and the Terraform.

What none of these can check is the live model and Spark's own reading of the
SQL, and that is what `databricks/notebooks/recommender_verify.py` is for.
"""

import random
import re
from decimal import Decimal
from pathlib import Path

import pytest

from chip_chat.data_gen import load_config
from chip_chat.databricks import catalog, gold, recommender, silver

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "databricks" / "src" / "chip_chat" / "databricks" / "recommender.py"
WRAPPER = (
    REPO / "databricks" / "src" / "chip_chat" / "databricks" / "recommender_model.py"
)
TRAIN = REPO / "databricks" / "notebooks" / "recommender_train.py"
PUBLISH = REPO / "databricks" / "notebooks" / "recommender_publish.py"
VERIFY = REPO / "databricks" / "notebooks" / "recommender_verify.py"
TERRAFORM = REPO / "infra" / "terraform" / "databricks_recommender.tf"
# The publish's Terraform, read only by the permission-vocabulary test below:
# it is #39's file, and the mistake it guards against was made in both at once.
PUBLISH_TERRAFORM = REPO / "infra" / "terraform" / "databricks_publish.tf"
VARIABLES = REPO / "infra" / "terraform" / "variables.tf"
OUTPUTS = REPO / "infra" / "terraform" / "outputs.tf"

NOTEBOOKS = (TRAIN, PUBLISH, VERIFY)

CONFIG = load_config()

ORDERS = "chip_chat.silver_synthetic.orders"
ORDER_ITEMS = "chip_chat.silver_synthetic.order_items"
MENU_ITEMS = "chip_chat.silver_harvested.menu_items"


def queries() -> tuple[str, ...]:
    """Return every SQL statement this module produces, filled."""
    return (
        recommender.training_query(ORDERS, ORDER_ITEMS),
        recommender.affinity_query("events", training_only=True),
        recommender.affinity_query("events", training_only=False),
        recommender.publish_query("scored", MENU_ITEMS, "7"),
    )


def statements(sql: str) -> str:
    """Return `sql` with its comments removed.

    These queries argue with the reader — several comments name the thing the
    query deliberately does *not* do — so a check that reads the text has to
    read the statements rather than the prose around them.
    """
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def code(notebook: Path) -> str:
    """Return a notebook with its markdown cells removed, for the same reason."""
    return "\n".join(
        line
        for line in notebook.read_text(encoding="utf-8").splitlines()
        if not line.startswith("# MAGIC")
    )


def pair(
    seed: str = "burrito",
    related: str = "salsa",
    co_orders: int = 200,
    orders_with_item: int = 400,
    orders_with_related: int = 500,
    orders: int = 2000,
) -> recommender.Affinity:
    """Return one fitted pair, with a strong lift by default."""
    return recommender.Affinity(
        item_id=seed,
        related_item_id=related,
        co_orders=co_orders,
        orders_with_item=orders_with_item,
        orders_with_related=orders_with_related,
        orders=orders,
    )


# --- Agreement with the rest of the lakehouse --------------------------------


def test_the_streams_are_the_ones_unity_catalog_has() -> None:
    """A copy, because this module may not import a sibling: it is uploaded as a
    flat workspace file and logged into a model version as `code_paths`."""
    assert recommender.STREAMS == catalog.STREAMS


def test_it_publishes_into_the_gold_layer_beside_the_marts() -> None:
    assert recommender.LAYER == gold.LAYER
    assert recommender.STREAM == gold.STREAM
    assert recommender.LAYER in catalog.LAYERS


def test_it_reads_silver_and_never_bronze() -> None:
    assert recommender.SOURCE_LAYER == gold.SOURCE_LAYER == silver.LAYER
    for sql in queries():
        assert "bronze" not in sql


@pytest.mark.parametrize("stream", catalog.STREAMS)
def test_the_schema_name_is_the_one_terraform_created(stream: catalog.Stream) -> None:
    assert recommender.schema_name(stream) == catalog.schema("gold", stream).name


def test_an_unknown_stream_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        recommender.schema_name("real")


def test_the_model_lives_in_the_schema_it_was_fitted_from() -> None:
    """Three-level, in `gold_synthetic`, so the grants that already cover the
    tables cover the model: a principal who may not read the synthetic
    population may not load the model fitted on it either. That is the whole
    difference between this registry and the workspace one."""
    assert catalog.schema(gold.LAYER, recommender.STREAM).name == (
        recommender.MODEL_SCHEMA
    )


def test_the_support_floor_is_the_marts_own() -> None:
    """Equal on purpose rather than by coincidence. The training run's
    full-history refit has to reproduce `item_affinity`, and a refit that kept
    pairs the mart dropped would disagree with it everywhere the two thresholds
    differed — a difference in bookkeeping, reported as a difference in the
    model."""
    assert recommender.MINIMUM_CO_ORDERS == gold.MINIMUM_CO_ORDERS


def test_the_settled_statuses_are_the_marts_and_the_generators() -> None:
    assert recommender.SETTLED_STATUSES == gold.SETTLED_STATUSES
    assert CONFIG.orders.settled_statuses == recommender.SETTLED_STATUSES


def test_the_entree_category_is_the_one_the_generator_orders_from() -> None:
    assert recommender.ENTREE_CATEGORY == gold.ENTREE_CATEGORY
    assert CONFIG.catalogue.entree_category == recommender.ENTREE_CATEGORY


def test_a_score_carries_the_same_precision_as_the_lift_it_shrinks() -> None:
    """A reader comparing a score to `item_affinity.lift` should not have to
    account for a difference in scale."""
    assert recommender.SCORE == gold.LIFT


def test_the_timestamp_column_is_the_one_every_mart_carries() -> None:
    """RFC-001 §10 serves a stale table *with its timestamp*, and a table with
    nowhere to put one cannot be served stale honestly."""
    assert recommender.DERIVED_AT == gold.DERIVED_AT
    assert recommender.DERIVED_AT in recommender.RECOMMENDATIONS.required


def test_the_reference_mart_is_one_of_the_four() -> None:
    assert gold.mart(recommender.REFERENCE_MART).stream == recommender.STREAM


def test_the_published_table_is_a_fifth_one_rather_than_a_changed_mart() -> None:
    """Issue #37 says "produce the item_affinity mart from the registered
    model"; #36 landed first and made it a materialized view whose determinism
    is its own acceptance criterion. So the mart stays, the refit has to
    reproduce it, and what the model publishes is a new table — which the
    serving path needed either way, because `item_affinity` is three columns
    wide and carries no `demo_id`."""
    assert recommender.MART not in gold.RFC_COLUMNS
    assert "demo_id" not in gold.column_names(gold.mart(recommender.REFERENCE_MART))
    assert "demo_id" in recommender.column_names()


def test_every_table_it_reads_is_one_silver_conforms() -> None:
    for name, stream in (
        ("orders", "synthetic"),
        ("order_items", "synthetic"),
        ("menu_items", "harvested"),
    ):
        assert silver.table(name).stream == stream


def test_nothing_reads_the_table_holding_the_editable_fields() -> None:
    """RFC-001 §04 answers PRD Q2 by containment, and the containment has to
    hold over everything downstream of silver rather than over the four marts
    only. A recommender that read a visitor's `stated_preferences` would break
    it while looking helpful."""
    assert recommender.FORBIDDEN_SOURCES == gold.FORBIDDEN_SOURCES
    for forbidden in recommender.FORBIDDEN_SOURCES:
        assert silver.table(forbidden).stream == "synthetic"
        for sql in queries():
            assert forbidden not in sql
        for notebook in NOTEBOOKS:
            assert forbidden not in code(notebook)


# --- Lift, and the shrinkage on top of it ------------------------------------


def test_lift_is_the_marts_arithmetic_to_the_marts_scale() -> None:
    """Four integers and one division, in exact arithmetic — the same
    expression `gold.item_affinity`'s SQL evaluates, which is what makes
    `item_affinity_agreement` a check of the model rather than of the
    arithmetic."""
    fitted = pair(co_orders=200, orders_with_item=400, orders_with_related=500)
    expected = (Decimal(200) * Decimal(2000)) / (Decimal(400) * Decimal(500))
    assert fitted.lift == expected.quantize(Decimal("0.000001"))
    assert fitted.lift.as_tuple().exponent == -6


def test_an_independent_pair_lifts_by_exactly_one() -> None:
    """One is lift's null value, and `MINIMUM_SCORE` is that number."""
    assert pair(co_orders=100, orders_with_item=200, orders_with_related=1000).lift == 1
    assert recommender.MINIMUM_SCORE == 1.0


def test_a_pair_neither_item_appears_in_is_refused() -> None:
    with pytest.raises(ValueError, match="no order at all"):
        _ = pair(orders_with_item=0).lift


def test_shrinkage_halves_a_pair_seen_exactly_that_many_times() -> None:
    """The definition of `SHRINKAGE`, stated as the assertion it is."""
    thin = pair(co_orders=recommender.SHRINKAGE)
    assert recommender.score(thin) == (thin.lift / 2).quantize(Decimal("0.000001"))


def test_more_evidence_at_the_same_lift_scores_higher() -> None:
    """What lift alone has no way to express, and the whole reason the score is
    not simply the lift."""
    thin = pair(co_orders=30, orders_with_item=60, orders_with_related=1000)
    thick = pair(co_orders=300, orders_with_item=600, orders_with_related=1000)
    assert thin.lift == thick.lift
    assert recommender.score(thin) < recommender.score(thick)


def test_shrinkage_only_ever_discounts() -> None:
    fitted = pair()
    assert recommender.score(fitted) < fitted.lift
    assert recommender.score(fitted, shrinkage=0) == fitted.lift


def test_a_negative_shrinkage_is_refused() -> None:
    with pytest.raises(ValueError, match="discounts thin pairs"):
        recommender.score(pair(), shrinkage=-1)


def test_a_share_is_a_share() -> None:
    assert recommender.seed_weight(3, 4) == Decimal("0.750000")
    with pytest.raises(ValueError, match="at least one order"):
        recommender.seed_weight(0, 0)
    with pytest.raises(ValueError, match="is not a share"):
        recommender.seed_weight(5, 4)


# --- What it recommends, and what it refuses ---------------------------------


def test_nothing_the_visitor_has_ever_ordered_is_recommended() -> None:
    """The strong exclusion, and the whole of the issue's second acceptance
    criterion. Not "nothing they order constantly" — nothing at all, including
    the thing they tried once, which is what turns the criterion into an
    emptiness assertion rather than an argument about a threshold."""
    tried_once = pair(related="chips", co_orders=300, orders_with_related=400)
    untried = pair(related="salsa", co_orders=300, orders_with_related=400)
    history = {"burrito": 30, "chips": 1}
    suggested = recommender.recommend(history, 40, [tried_once, untried])
    assert [item.item_id for item in suggested] == ["salsa"]


def test_a_pair_at_or_below_independence_is_not_suggested() -> None:
    """Below lift's null value two items are ordered *instead of* each other,
    and recommending one is worse than saying nothing."""
    against = pair(co_orders=100, orders_with_item=400, orders_with_related=1000)
    assert against.lift < 1
    assert recommender.recommend({"burrito": 40}, 40, [against]) == ()


def test_the_floor_is_about_the_pair_and_not_about_the_visitor() -> None:
    """A visitor who orders the seed in a quarter of their orders still gets the
    recommendation, because "these two items go together" and "this visitor
    orders the first one often" are different claims and only the first is a
    reason to stay silent."""
    strong = pair()
    assert recommender.score(strong) >= recommender.MINIMUM_SCORE
    suggested = recommender.recommend({"burrito": 10}, 40, [strong])
    assert [item.item_id for item in suggested] == ["salsa"]
    assert suggested[0].seed_share == Decimal("0.250000")
    assert suggested[0].score < recommender.MINIMUM_SCORE


def test_a_thin_pair_is_dropped_before_it_is_scored() -> None:
    """The support floor is applied inside the scoring too, and not only in the
    SQL that fits the model — a model scored at one threshold and trained at
    another has a support floor nobody wrote down."""
    thin = pair(co_orders=recommender.MINIMUM_CO_ORDERS - 1, orders_with_related=30)
    assert thin.lift > recommender.MINIMUM_SCORE
    assert recommender.recommend({"burrito": 40}, 40, [thin]) == ()


def test_a_visitor_with_no_history_gets_an_honest_absence() -> None:
    """Rather than a popularity fallback, which is precisely the generic
    top-sellers list PRD P2 exists to rule out and would be indistinguishable,
    in the served table, from a real recommendation."""
    assert recommender.recommend({}, 0, [pair()]) == ()
    assert recommender.recommend({"unknown": 5}, 5, [pair()]) == ()


def test_the_ranks_are_contiguous_and_capped() -> None:
    fitted = [
        pair(related=f"item-{index}", orders_with_related=400 + index)
        for index in range(recommender.TOP_K + 3)
    ]
    suggested = recommender.recommend({"burrito": 40}, 40, fitted)
    assert len(suggested) == recommender.TOP_K
    assert [item.rank for item in suggested] == list(range(1, recommender.TOP_K + 1))
    assert [item.score for item in suggested] == sorted(
        (item.score for item in suggested), reverse=True
    )


def test_no_recommendations_at_all_is_refused_as_a_setting() -> None:
    with pytest.raises(ValueError, match="at least one recommendation"):
        recommender.recommend({"burrito": 1}, 1, [pair()], top_k=0)


def test_the_same_input_in_any_order_gives_the_same_answer() -> None:
    """`gold.py`'s rule, applied here: an ordering that is reproducible only
    until the files underneath are rewritten produces a visitor's
    recommendations changing for no reason a month later."""
    fitted = [
        pair(
            seed=f"seed-{index % 3}",
            related=f"item-{index}",
            co_orders=100 + index,
            orders_with_related=400,
        )
        for index in range(20)
    ]
    history = {f"seed-{index}": 10 for index in range(3)}
    expected = recommender.recommend(history, 30, fitted)
    shuffler = random.Random(20260827)
    for _ in range(10):
        shuffled = list(fitted)
        shuffler.shuffle(shuffled)
        assert recommender.recommend(history, 30, shuffled) == expected


def test_a_tie_between_two_seeds_prefers_the_entree() -> None:
    """ "Your usual is a bag of chips" is a worse sentence than one naming the
    thing the visitor thinks of as their order, and the preference is a property
    of the seed rather than of the order the pairs arrived in."""
    fitted = [
        pair(seed="chips", related="salsa"),
        pair(seed="burrito", related="salsa"),
    ]
    history = {"chips": 20, "burrito": 20}
    suggested = recommender.recommend(history, 40, fitted, entrees=frozenset({"burrito"}))
    assert suggested[0].seed_item_id == "burrito"
    assert recommender.recommend(history, 40, fitted)[0].seed_item_id == "burrito"
    assert (
        recommender.recommend(history, 40, fitted, entrees=frozenset({"chips"}))[
            0
        ].seed_item_id
        == "chips"
    )


def test_the_seed_is_the_argmax_and_not_a_contributor() -> None:
    """A sum of seeds names no seed, and the rationale has to name one. The
    strongest pair wins, and the score published beside it is that pair's."""
    weak = pair(seed="chips", related="salsa", co_orders=30, orders_with_related=900)
    strong = pair(seed="burrito", related="salsa")
    suggested = recommender.recommend({"chips": 20, "burrito": 20}, 40, [weak, strong])
    assert suggested[0].seed_item_id == "burrito"
    assert suggested[0].score == (
        recommender.seed_weight(20, 40) * recommender.score(strong)
    ).quantize(Decimal("0.000001"))


# --- How it is judged ---------------------------------------------------------


def test_a_popularity_list_wins_on_hits_and_loses_on_novelty() -> None:
    """PRD requirement P2, as a fixture.

    The requirement is recommendations grounded in the visitor's actual ordering
    behaviour *rather than generic popularity*, and it adds that a global
    top-sellers list does not satisfy it **even if it scores well**. This is
    what "even if it scores well" looks like: three visitors who each re-order
    their staple and try one new thing. Popularity nails the staple and is novel
    to nobody; the affinity model misses the staple and is novel to everybody.

    Both sides are measured by `hit_rates`, called twice — a comparison whose
    two halves were measured by two functions is a comparison of the functions.
    """
    tried = {"a": ["staple"], "b": ["staple"], "c": ["staple"]}
    holdout = {
        "a": ["staple", "new-a"],
        "b": ["staple", "new-b"],
        "c": ["staple", "new-c"],
    }
    popularity = {visitor: ["staple"] for visitor in tried}
    affinity = {"a": ["new-a"], "b": ["new-b"], "c": ["new-c"]}

    popular_hit, popular_novel, scored = recommender.hit_rates(popularity, holdout, tried)
    model_hit, model_novel, _ = recommender.hit_rates(affinity, holdout, tried)

    assert scored == 3
    assert popular_hit == 1
    assert popular_novel == 0
    assert model_hit == 1
    assert model_novel == 1
    assert recommender.beats_baseline(model_novel, popular_novel)
    assert not recommender.beats_baseline(popular_novel, model_novel)


def test_a_visitor_with_no_holdout_is_not_in_the_denominator() -> None:
    """A hit rate over four visitors is not a measurement, and one whose
    denominator silently includes visitors who could not be scored is worse than
    that — it is a measurement that looks like one."""
    rate, novel, scored = recommender.hit_rates(
        {"a": ["x"], "b": ["y"]}, {"a": ["x"]}, {"a": []}
    )
    assert (rate, novel, scored) == (Decimal(1), Decimal(1), 1)
    assert recommender.hit_rates({"a": ["x"]}, {}, {}) == (Decimal(0), Decimal(0), 0)


def test_the_alias_does_not_move_on_a_tie() -> None:
    """Two numbers from the same holdout differ in the last place for reasons
    that are not improvements, and an alias that moves on noise means nothing."""
    assert not recommender.beats_baseline(0.5, 0.5)
    assert not recommender.beats_baseline(0.5, 0.5 - recommender.MINIMUM_MARGIN / 2)
    assert recommender.beats_baseline(0.5, 0.5 - recommender.MINIMUM_MARGIN)


def test_a_negative_margin_would_promote_a_run_that_lost() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        recommender.beats_baseline(0.1, 0.9, margin=-1.0)


def test_the_baseline_breaks_its_ties_on_the_item() -> None:
    assert recommender.popular_items({"b": 5, "a": 5, "c": 1}, 2) == ("a", "b")
    assert recommender.popular_items({"a": 0}) == ()
    with pytest.raises(ValueError, match="at least one item"):
        recommender.popular_items({"a": 1}, 0)


def test_every_metric_is_named_once_and_findable() -> None:
    names = [measure.name for measure in recommender.METRICS]
    assert len(names) == len(set(names))
    for name in names:
        assert recommender.metric(name).name == name
    with pytest.raises(KeyError, match="no metric"):
        recommender.metric("accuracy")


def test_the_promotion_metric_and_its_baseline_are_both_logged() -> None:
    """The rule is about novel hit rate against a baseline, so a run that logged
    one without the other would record a decision nobody could check."""
    logged = {measure.name for measure in recommender.METRICS}
    assert recommender.NOVEL_HIT_RATE.name in logged
    assert recommender.BASELINE_NOVEL_HIT_RATE.name in logged
    assert recommender.HIT_RATE.name in logged
    assert recommender.BASELINE_HIT_RATE.name in logged


def test_every_hyperparameter_is_named_once_and_explained() -> None:
    settings = recommender.hyperparameters()
    names = [setting.name for setting in settings]
    assert len(names) == len(set(names))
    for setting in settings:
        assert setting.why.strip()


def test_the_fitted_numbers_are_the_ones_the_run_logs() -> None:
    """A parameter that governs the model and is not logged is a version nobody
    can reproduce."""
    logged = {setting.name: setting.value for setting in recommender.hyperparameters()}
    assert logged["minimum_co_orders"] == recommender.MINIMUM_CO_ORDERS
    assert logged["shrinkage"] == recommender.SHRINKAGE
    assert logged["minimum_score"] == recommender.MINIMUM_SCORE
    assert logged["top_k"] == recommender.TOP_K
    assert logged["holdout_fraction"] == recommender.HOLDOUT_FRACTION
    assert logged["minimum_margin"] == recommender.MINIMUM_MARGIN


# --- The sentence -------------------------------------------------------------


def test_the_rationale_is_the_shape_the_issue_asks_for() -> None:
    """*"You order barbacoa most weeks and people who do tend to like the
    tomatillo-red chili salsa"* is the shape of output that works in a
    conversation, and this is that sentence."""
    written = recommender.rationale(
        "the Barbacoa Burrito", Decimal("0.62"), "the Tomatillo Red-Chili Salsa"
    )
    assert written == (
        "You order the Barbacoa Burrito in most of your orders, and people who "
        "do tend to add the Tomatillo Red-Chili Salsa."
    )
    assert written.startswith(recommender.RATIONALE_LEAD)
    assert recommender.RATIONALE_JOIN in written
    assert written.endswith(recommender.RATIONALE_TAIL)


def test_a_rationale_names_two_items_or_it_is_refused() -> None:
    with pytest.raises(ValueError, match="empty name"):
        recommender.rationale("", Decimal("0.5"), "salsa")
    with pytest.raises(ValueError, match="empty name"):
        recommender.rationale("burrito", Decimal("0.5"), "")


def test_the_phrases_read_a_share_and_nothing_else() -> None:
    floors = [candidate.floor for candidate in recommender.SHARE_PHRASES]
    assert floors == sorted(floors, reverse=True)
    assert floors[-1] == 0
    assert all(0 <= floor <= 1 for floor in floors)
    with pytest.raises(ValueError, match=r"a share is in \[0, 1\]"):
        recommender.phrase(1.5)


@pytest.mark.parametrize("candidate", recommender.SHARE_PHRASES, ids=lambda p: p.words)
def test_every_phrase_is_reachable(candidate: recommender.Phrase) -> None:
    assert recommender.phrase(candidate.floor) is candidate


def test_the_short_rationale_leaves_room_for_two_real_item_names() -> None:
    """The issue asks for a *short* rationale, and short is only enforceable as
    a number — but a number that left no room for the catalogue's own names
    would fail the update rather than shorten the sentence."""
    fixed = (
        len(recommender.RATIONALE_LEAD)
        + max(len(candidate.words) for candidate in recommender.SHARE_PHRASES)
        + len(recommender.RATIONALE_JOIN)
        + len(recommender.RATIONALE_TAIL)
        + 1
    )
    assert recommender.MAX_RATIONALE_CHARS - fixed >= 60


def test_the_sql_renders_the_same_sentence_the_python_does() -> None:
    """Neither is the authority: the published rationale is rendered in SQL,
    because the item names live in `menu_items` and joining them is a join, and
    this is what holds that SQL to the definition next to it."""
    expression = recommender.rationale_expression(
        "seed.name", "r.seed_share", "item.name"
    )
    assert f"'{recommender.RATIONALE_LEAD}'" in expression
    assert f"'{recommender.RATIONALE_JOIN}'" in expression
    assert f"'{recommender.RATIONALE_TAIL}'" in expression
    for candidate in recommender.SHARE_PHRASES:
        assert f"WHEN r.seed_share >= {candidate.floor} THEN" in expression
        assert f"'{candidate.words}'" in expression


def test_no_phrase_would_have_to_be_escaped_into_the_sql() -> None:
    """The expression is assembled as a string, so an apostrophe in a phrase
    would terminate a literal and produce SQL that parses into something else.
    Better to forbid it than to escape it in a template nobody re-reads."""
    for candidate in recommender.SHARE_PHRASES:
        assert "'" not in candidate.words
    for fixed in (
        recommender.RATIONALE_LEAD,
        recommender.RATIONALE_JOIN,
        recommender.RATIONALE_TAIL,
    ):
        assert "'" not in fixed


# --- The published table ------------------------------------------------------


def test_the_columns_are_the_declared_ones_in_order() -> None:
    assert recommender.column_names() == (
        "demo_id",
        "rank",
        "item_id",
        "seed_item_id",
        "seed_share",
        "score",
        "rationale",
        "model_version",
        recommender.DERIVED_AT,
    )


def test_every_column_says_what_it_means() -> None:
    for column in recommender.RECOMMENDATIONS.columns:
        assert column.why.strip()
        assert column.sql_type.strip()


def test_every_required_column_is_a_column() -> None:
    assert set(recommender.RECOMMENDATIONS.required) <= set(recommender.column_names())


def test_it_carries_the_column_a_row_access_policy_compares() -> None:
    """Issue #43 protects visitor-scoped tables with row access policies, and a
    policy needs a column to compare. Unlike `item_affinity`, this table is
    about a visitor."""
    assert "demo_id" in recommender.column_names()
    assert "demo_id" in recommender.RECOMMENDATIONS.required


def test_every_expectation_is_named_once_and_argued_for() -> None:
    checks = recommender.expectations()
    names = [check.name for check in checks]
    assert len(names) == len(set(names))
    for check in checks:
        assert check.why.strip()


def test_every_expectation_reads_only_this_tables_columns() -> None:
    """An expectation over a column the table does not publish is a check that
    fails the update for a reason nobody can act on."""
    known = set(recommender.column_names())
    keywords = {"AND", "OR", "NOT", "IS", "NULL", "length"}
    for check in recommender.expectations():
        for token in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", check.constraint):
            assert token in known or token in keywords, check.name


def test_the_required_columns_became_expectations() -> None:
    names = {check.name for check in recommender.expectations()}
    for column in recommender.RECOMMENDATIONS.required:
        assert f"{column}_is_present" in names


def test_the_independence_floor_survives_into_the_table() -> None:
    """The floor is applied to the pair, and the pair's own score is not a
    column — but `score / seed_share` is exactly it, so the check is still one
    a reader can run against the published rows."""
    check = next(
        item for item in recommender.expectations() if item.name == "clears_independence"
    )
    assert str(recommender.MINIMUM_SCORE) in check.constraint
    assert "seed_share" in check.constraint


def test_the_rationale_length_is_enforced_and_not_merely_tested() -> None:
    check = next(
        item
        for item in recommender.expectations()
        if item.name == "explains_itself_briefly"
    )
    assert str(recommender.MAX_RATIONALE_CHARS) in check.constraint


# --- The queries --------------------------------------------------------------


def test_every_placeholder_is_filled() -> None:
    """A template that reached the cluster with a `{name}` in it is a query that
    fails minutes into a run."""
    for sql in queries():
        assert "{" not in sql
        assert "}" not in sql


def test_the_settled_rule_reaches_the_query_that_applies_it() -> None:
    training = recommender.training_query(ORDERS, ORDER_ITEMS)
    for status in recommender.SETTLED_STATUSES:
        assert f"'{status}'" in training


def test_the_support_floor_reaches_the_query_that_applies_it() -> None:
    for training_only in (True, False):
        sql = recommender.affinity_query("events", training_only=training_only)
        assert f">= {recommender.MINIMUM_CO_ORDERS}" in sql


def test_the_two_fits_differ_only_in_their_window() -> None:
    """One statement, one predicate changed, so the training fit and the
    registered refit cannot differ in anything else — which is what makes
    `item_affinity_agreement` about the window rather than about the query."""
    trained = recommender.affinity_query("events", training_only=True)
    whole = recommender.affinity_query("events", training_only=False)
    assert trained.replace("WHERE is_training", "WHERE true") == whole


def test_no_tie_is_broken_on_arrival_order() -> None:
    """`gold.py`'s rule. `first()` over an unordered group is reproducible only
    until the files underneath it are rewritten."""
    for sql in queries():
        cleaned = statements(sql)
        assert "first(" not in cleaned
        assert "any_value(" not in cleaned


def test_only_the_published_row_reads_the_wall_clock() -> None:
    """`derived_at` is the one wall-clock value, for the reason it is the only
    one in `gold.py`: everything else that needs a "now" is measured against the
    data, so a rebuild from unchanged silver reproduces the row."""
    publish = recommender.publish_query("scored", MENU_ITEMS, "7")
    assert statements(publish).count("current_timestamp()") == 1
    for sql in queries():
        if sql != publish:
            assert "current_timestamp()" not in statements(sql)


def test_the_split_instant_comes_out_of_the_data() -> None:
    training = statements(recommender.training_query(ORDERS, ORDER_ITEMS))
    assert "percentile_approx" in training
    assert "current_timestamp()" not in training


def test_the_publish_query_produces_exactly_the_declared_columns() -> None:
    publish = recommender.publish_query("scored", MENU_ITEMS, "7")
    for column in recommender.column_names():
        assert f" AS {column}" in publish or f"r.{column}," in publish


def test_a_published_row_names_the_version_that_made_it() -> None:
    assert "'7'" in recommender.publish_query("scored", MENU_ITEMS, "7")
    with pytest.raises(ValueError, match="names the version"):
        recommender.publish_query("scored", MENU_ITEMS, "")


def test_a_holdout_of_nothing_or_everything_is_refused() -> None:
    for fraction in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="fraction of the history"):
            recommender.training_query(ORDERS, ORDER_ITEMS, holdout_fraction=fraction)


def test_a_pair_needs_at_least_one_co_order_to_have_a_lift() -> None:
    with pytest.raises(ValueError, match="at least one co-order"):
        recommender.affinity_query("events", training_only=True, minimum_co_orders=0)


def test_the_json_the_model_returns_is_the_recommendation_it_makes() -> None:
    """The publish notebook explodes the model's output with this schema, so a
    field added to `Recommendation` and not to `SCORED_FIELDS` would be a column
    that silently stopped arriving."""
    fields = [name for name, _ in recommender.SCORED_FIELDS]
    assert fields == [
        name for name in recommender.Recommendation.__dataclass_fields__ if name
    ]
    assert recommender.scored_schema().startswith("array<struct<")


def test_the_two_decimals_cross_the_boundary_as_strings() -> None:
    """JSON has one number type and it is a double. A decimal that travelled as
    a JSON number would come back with a rounding error in the last place, and
    DECIMAL(12,6) exists in this layer so that a rebuild reproduces the row."""
    types = dict(recommender.SCORED_FIELDS)
    assert types["score"] == "string"
    assert types["seed_share"] == "string"


# --- The notebooks ------------------------------------------------------------


@pytest.mark.parametrize("notebook", NOTEBOOKS, ids=lambda path: path.name)
def test_the_notebook_is_a_notebook(notebook: Path) -> None:
    assert notebook.read_text(encoding="utf-8").startswith("# Databricks notebook source")


@pytest.mark.parametrize("notebook", NOTEBOOKS, ids=lambda path: path.name)
def test_the_notebook_gets_its_thresholds_from_the_module(notebook: Path) -> None:
    """The notebooks are loops and print statements. A threshold retyped into
    one is a threshold that can drift from the SQL that applies it."""
    body = code(notebook)
    assert "import recommender" in body
    for setting in recommender.hyperparameters():
        if isinstance(setting.value, bool):
            continue
        assert f"= {setting.value}\n" not in body


def test_the_training_run_registers_into_unity_catalog() -> None:
    body = code(TRAIN)
    assert 'mlflow.set_registry_uri("databricks-uc")' in body
    assert "registered_model_name=MODEL" in body
    assert "recommender.MODEL_SCHEMA" in body


def test_the_training_run_moves_an_alias_and_never_a_stage() -> None:
    """Unity Catalog replaced the Workspace Model Registry's Staging/Production
    transitions with aliases, so `transition_model_version_stage` is not a call
    this registry has — the same judgement this project made about `import
    dlt`."""
    body = code(TRAIN)
    assert "set_registered_model_alias" in body
    assert "transition_model_version_stage" not in body
    assert "recommender.CHAMPION_ALIAS" in body


def test_the_alias_only_moves_when_the_run_beat_the_baseline() -> None:
    body = code(TRAIN)
    assert "recommender.beats_baseline(" in body
    assert body.index("recommender.beats_baseline(") < body.index(
        "set_registered_model_alias"
    )


def test_the_training_run_logs_every_metric_and_every_parameter() -> None:
    body = code(TRAIN)
    assert "recommender.hyperparameters()" in body
    declared = {
        name
        for name, value in vars(recommender).items()
        if isinstance(value, recommender.Metric)
    }
    assert len(declared) == len(recommender.METRICS)
    for constant in declared:
        assert f"recommender.{constant}" in body


def test_the_training_run_compares_its_refit_to_the_published_mart() -> None:
    """Issue #37's "produce the item_affinity mart from the registered model",
    inverted into a check rather than into a rebuild."""
    body = code(TRAIN)
    assert "recommender.REFERENCE_MART" in body
    assert "recommender.AGREEMENT" in body


def test_the_publish_task_loads_the_alias_and_not_a_version() -> None:
    """The alias is the deployment: a run that could not beat the baseline left
    it where it was, so this task republishes the same recommendations rather
    than worse ones."""
    body = code(PUBLISH)
    assert "recommender.CHAMPION_ALIAS" in body
    assert "models:/" in body
    assert not re.search(r"models:/[^\"']*/\d", body)


def test_the_publish_task_replaces_the_table_in_one_commit() -> None:
    """A reader mid-publish sees last night's recommendations or tonight's and
    never an empty serving path, which matters more here than in the
    pipeline-built marts: a job can die between two statements."""
    body = code(PUBLISH)
    assert "CREATE OR REPLACE TABLE" in body
    assert "TRUNCATE" not in body.upper()
    assert "DELETE FROM" not in body.upper()


def test_the_publish_task_applies_every_expectation() -> None:
    body = code(PUBLISH)
    assert "recommender.expectations()" in body
    assert "raise AssertionError" in body


def test_the_verify_notebook_checks_all_four_criteria() -> None:
    body = code(VERIFY)
    assert "get_model_version_by_alias" in body
    assert "quartz_cron_expression" in body
    assert "MAX_RATIONALE_CHARS" in body
    assert "recommender.RATIONALE_LEAD" in body


def test_the_verify_notebook_checks_the_exclusion_over_the_whole_population() -> None:
    """The issue says "a sample of personas"; the exclusion is cheap enough to
    assert over everybody, and a sample is what lets one visitor's wrong answer
    through."""
    body = code(VERIFY)
    assert "ORDER_ITEMS" in body
    assert "persona_fixtures" in body or "FIXTURES" in body


def test_the_verify_notebook_asserts_it_is_not_a_top_sellers_list() -> None:
    body = code(VERIFY)
    assert "distinct_firsts" in body


# --- The MLflow wrapper, read as text ----------------------------------------


def test_the_wrapper_is_an_mlflow_model_that_delegates() -> None:
    """It cannot be imported here: MLflow is deliberately not in this lockfile,
    because adding it would put a very large dependency into every developer's
    virtualenv to satisfy a file nothing in CI imports."""
    body = WRAPPER.read_text(encoding="utf-8")
    assert "mlflow.pyfunc.PythonModel" in body
    assert "recommender.recommend(" in body
    assert "recommender.Affinity(" in body


def test_the_wrapper_declares_a_signature() -> None:
    """Unity Catalog requires one, and the requirement is a good one: the
    columns are the whole interface, and writing them down is what stops a
    scoring job from discovering them by trial."""
    body = WRAPPER.read_text(encoding="utf-8")
    assert "ModelSignature" in body
    assert "history_json" in body
    assert "recommendations_json" in body


def test_the_wrapper_holds_no_threshold_of_its_own() -> None:
    """Every number is next door. A threshold here would be one the tests above
    cannot reach and the training run does not log."""
    body = WRAPPER.read_text(encoding="utf-8")
    for setting in recommender.hyperparameters():
        if isinstance(setting.value, bool):
            continue
        assert str(setting.value) not in body


def test_the_wrapper_imports_the_module_the_way_the_cluster_lays_it_out() -> None:
    """MLflow copies `code_paths` into a model version as a flat directory, and
    Terraform uploads the workspace files the same way, so on every surface that
    loads this model the module next door is called `recommender`."""
    body = WRAPPER.read_text(encoding="utf-8")
    assert "\nimport recommender\n" in body
    assert "from chip_chat.databricks import recommender" not in body


# --- The Terraform ------------------------------------------------------------


def test_terraform_uploads_both_modules_and_all_three_notebooks() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    assert "databricks/src/chip_chat/databricks/recommender.py" in body
    assert "databricks/src/chip_chat/databricks/recommender_model.py" in body
    for notebook in NOTEBOOKS:
        assert f"databricks/notebooks/{notebook.name}" in body


def test_terraform_declares_the_registered_model_the_module_names() -> None:
    """Created by Terraform and not by the notebook, for the reason
    `databricks_catalog.tf` gives about schemas: ownership and grants are cheap
    to set on an empty object and tedious to retrofit onto a populated one."""
    body = TERRAFORM.read_text(encoding="utf-8")
    assert 'resource "databricks_registered_model" "recommender"' in body
    assert f'name         = "{recommender.MODEL_NAME}"' in body
    assert f'databricks_schema.medallion["{recommender.MODEL_SCHEMA}"]' in body


def test_terraform_names_the_experiment_the_module_names() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    assert 'resource "databricks_mlflow_experiment" "recommender"' in body
    assert f"/{recommender.EXPERIMENT}" in body


def test_the_experiments_directory_is_declared_rather_than_assumed() -> None:
    """The first apply of this file failed on it, so it is a test.

    `databricks_notebook` and `databricks_workspace_file` create the
    directories on the way to their path; the MLflow experiment API does not,
    and returns `Parent directory does not exist` instead. Nothing else in this
    repository writes an object under `experiments/`, so no other resource
    brings it into being as a side effect.

    The experiment has to take its name *from* the directory resource rather
    than rebuild the string, because a `depends_on` somebody deletes while
    tidying is an ordering constraint that stops existing quietly. A reference
    is one Terraform cannot drop.
    """
    body = TERRAFORM.read_text(encoding="utf-8")
    assert 'resource "databricks_directory" "recommender_experiments"' in body
    assert (
        'name = "${databricks_directory.recommender_experiments.path}'
        f'/{recommender.EXPERIMENT}"' in body
    )


def test_no_job_is_granted_a_permission_only_a_pipeline_has() -> None:
    """The other thing the first apply was rejected for.

    Jobs and pipelines do not share a permission vocabulary. A pipeline takes
    `CAN_RUN`; a job takes only `CAN_MANAGE`, `CAN_MANAGE_RUN`, `CAN_VIEW` and
    `IS_OWNER`, and asking for the pipeline word on a job is refused by the API
    rather than downgraded. The two files this repository grants the app tier a
    start-it permission in are the recommender's and the publish's, and both
    grant it on a job.

    Checked as "this file has no `CAN_RUN` at all" rather than by parsing the
    blocks, because neither file declares a pipeline: any `CAN_RUN` appearing
    here later is on a job by construction, and would fail an apply nobody runs
    in CI.
    """
    for path in (TERRAFORM, PUBLISH_TERRAFORM):
        body = path.read_text(encoding="utf-8")
        assert 'resource "databricks_pipeline"' not in body, path.name
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # the comment explaining this is allowed to say it
            assert "CAN_RUN" not in stripped, (path.name, stripped)


def test_retraining_is_a_scheduled_job() -> None:
    """Issue #37's fourth acceptance criterion. A cron expression somebody can
    read, declared in the Terraform rather than set by hand in a workspace."""
    body = TERRAFORM.read_text(encoding="utf-8")
    assert "quartz_cron_expression" in body
    assert "timezone_id" in body


def test_the_schedule_ships_paused_and_is_one_variable_from_running() -> None:
    """The rest of this directory says nothing in the workspace should be able
    to start spending on its own, and #37 says retraining is scheduled. Both
    hold: the cron is declared where a person can review it, and it does not
    fire until the variable is set."""
    body = TERRAFORM.read_text(encoding="utf-8")
    assert "var.databricks_recommender_schedule_enabled" in body
    assert '"UNPAUSED" : "PAUSED"' in body
    variables = VARIABLES.read_text(encoding="utf-8")
    assert 'variable "databricks_recommender_schedule_enabled"' in variables
    assert re.search(
        r'variable "databricks_recommender_schedule_enabled".*?default\s*=\s*false',
        variables,
        re.DOTALL,
    )


def test_the_job_trains_before_it_publishes() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    assert 'task_key        = "train"' in body
    assert 'task_key        = "publish"' in body
    assert body.index('task_key        = "train"') < body.index(
        'task_key        = "publish"'
    )
    assert re.search(r"depends_on\s*\{\s*task_key = \"train\"\s*\}", body)


def test_the_job_runs_on_a_single_node_cluster_that_stops() -> None:
    """The cost guardrail every other Databricks file in this repository
    applies: a single-node job cluster, never an always-on all-purpose one."""
    body = TERRAFORM.read_text(encoding="utf-8")
    assert "databricks_cluster_policy.job_single_node.id" in body
    assert "num_workers   = 0" in body
    assert '"ResourceClass" = "SingleNode"' in body
    assert "databricks_model_serving" not in body


def test_the_verify_job_is_separate_and_read_only() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    assert 'resource "databricks_job" "recommender_verify"' in body
    assert "job_name = databricks_job.recommender[0].name" in body


def test_the_jobs_and_the_model_are_reachable_from_the_outputs() -> None:
    body = OUTPUTS.read_text(encoding="utf-8")
    assert 'output "databricks_recommender_job_id"' in body
    assert 'output "databricks_recommender_verify_job_id"' in body
    assert 'output "databricks_recommender_model"' in body
