"""The recommender: what it scores, what it refuses to say, and how it is judged.

Issue #37 asks for a modest model tracked properly. The modesty is the point --
this is a co-occurrence model, not a modelling exercise -- and the two things
that are *not* modest are the ones the issue actually cares about. The MLflow
tracking and Unity Catalog registry path is exercised for real, with a version
and an alias that only moves when a run earns it. And the recommendations are
grounded in the visitor's own ordering behaviour, which PRD requirement P2 is
explicit is not the same thing as a global top-sellers list *even if the
top-sellers list scores well*. §"What the holdout measures" below is that
sentence turned into two numbers.

Same shape as :mod:`chip_chat.databricks.gold`: the notebooks
(``recommender_train.py``, ``recommender_publish.py``, ``recommender_verify.py``)
are loops and print statements, and every decision lives here where
``databricks/tests/test_recommender.py`` can assert it without a cluster.

**This module imports nothing but the standard library, and that is
load-bearing** -- the reason it is load-bearing in ``bronze.py``, ``silver.py``
and ``gold.py``, plus one this issue adds. Terraform uploads this exact file
beside the notebooks and each notebook puts its directory on ``sys.path``, so it
has to import two ways: as ``chip_chat.databricks.recommender`` under pytest and
as a flat top-level ``recommender`` on the driver. The addition is that MLflow
logs this file as ``code_paths``, so it is loaded again inside a *served* model,
in a process that has whatever the serving image has and nothing else. A
dependency here would be a dependency of every future scoring environment.

:mod:`chip_chat.databricks.recommender_model` is the one file that does import
MLflow. It is four methods of ``mlflow.pyfunc.PythonModel`` and delegates every
decision back here.

## What #37 asked for, and what landed

The issue says *produce the ``item_affinity`` mart from the registered model*.
Issue #36 landed first and made ``item_affinity`` a materialized view whose
determinism is ``gold_verify.py``'s fifth criterion, and
``docs/gold-marts.md`` §8 already records the division: **``item_affinity`` is
this issue's training input.** Rather than move a mart out from under a passing
criterion, the relationship is inverted and made into a check:

* ``item_affinity`` stays exactly as #36 built it, and the model's full-history
  refit has to **reproduce** it. :data:`AGREEMENT` is the metric, and a refit
  that disagrees with the published mart is a model that has quietly stopped
  being a co-occurrence model.
* What the model publishes is :data:`MART` -- ``recommendations``, one row per
  visitor per suggestion, carrying the rationale. RFC-001 §06 says
  ``get_recommendations`` returns *ranked items with rationale*, and
  ``item_affinity`` has three columns and no ``demo_id``, so the serving path
  needed a visitor-scoped table from either reading of the issue.

The serving path therefore reads a table and never calls a model, which is the
property the issue's third bullet is really about.

## What a score is

Two numbers, and the second is the one that stops a thin pair from arriving at a
visitor as a suggestion.

**Lift**, exactly as ``gold.item_affinity`` defines it: ``P(both) / (P(a) ·
P(b))`` over settled orders, evaluated as four integers and one division. One
means independent; above one means the two items are ordered together more often
than chance; below one means they are ordered *instead of* each other, which is
a reason to say nothing rather than a weak reason to speak.

**Shrinkage**, which lift on its own has no way to express. A pair seen 25 times
and a pair seen 2,500 times can carry the same lift and are not the same
evidence, so the score is ``lift · co / (co + SHRINKAGE)``: an empirical-Bayes
shrink toward independence that costs a thin pair most of its lift and a thick
one almost none. :data:`SHRINKAGE` is the number of co-orders at which a pair
keeps half of what it claims. It is a hyperparameter, it is logged to MLflow
with everything else, and :func:`score` is where it is applied.

A pair still needs :data:`MINIMUM_CO_ORDERS` co-orders before it is looked at
at all -- the same 25 ``gold.MINIMUM_CO_ORDERS`` applies, asserted equal in the
tests, because a model trained on a table filtered at one threshold and scoring
at another is a model whose support floor is whichever is larger and nobody
wrote down.

## What the visitor's own history does to it

A visitor is a bag of seeds: every item they have settled-ordered, weighted by
:func:`seed_weight` -- the share of their orders that contained it. A candidate's
score is the **maximum** over seeds of ``seed_weight · score(seed, candidate)``,
never the sum.

That is a deliberate loss of a little accuracy for the thing the issue asks for
by name. The rationale has to be explainable in one sentence, and a sum names no
seed: it would produce *people who order the things you order tend to like this*,
which is true of a popularity list too. A maximum has an argmax, the argmax is a
real item the visitor really orders, and :func:`rationale` writes the sentence
about it. ``max`` is also stable under ties in a way ``sum`` is not -- see
"Ties break on the data" below.

## What it refuses to say

**Anything the visitor has ever ordered.** Not "anything they order
constantly" -- everything, including the item they tried once eleven months ago.

The issue asks two things of the exclusion: that recommendations are for things
the customer has *genuinely not tried*, and that the model does not recommend
what they *already order constantly*. A share threshold satisfies the second and
argues about the first, and it makes both an argument about where the threshold
sits. Excluding the whole history satisfies both, makes the second a strict
consequence of the first, and turns the acceptance criterion into an emptiness
assertion: ``recommendations`` joined to the visitor's order history must return
no rows. ``recommender_verify.py`` runs exactly that join.

The cost is real and worth naming: a visitor who has tried a thing once and
would happily be reminded of it will not be. That is the trade this project
wants -- a suggestion for something already familiar reads, in conversation, as
the assistant not having looked.

**Any pair at or below independence.** :data:`MINIMUM_SCORE` is 1.0, which is
lift's own null value. A shrunk lift below one is a pair that is ordered
together *less* than chance; recommending it is worse than saying nothing.

The floor is applied to the **pair**, before the visitor's own share weights it.
That distinction is the difference between "these two items do not go together"
and "this visitor does not order the first one very often", and only the first
is a reason to stay silent. Because both numbers are published -- ``score`` and
``seed_share`` -- the floor is still checkable in the table: ``score >=
MINIMUM_SCORE * seed_share`` is one of :func:`expectations`.

**Anything at all, for a visitor with nothing to go on.** A visitor with no
settled orders, or none whose items have a surviving pair, gets no rows rather
than a popularity fallback. That is ``usual_order``'s call in #36 -- a visitor
with no usual gets an honest absence -- made again for the same reason, and PRD
P2 makes it sharper here: a popularity fallback is precisely the generic
top-sellers list the requirement exists to rule out, and it would be
indistinguishable, in the served table, from a real recommendation.

## What the holdout measures

Fitting on everything and reporting how well the fit fits is not a measurement,
so the run splits on time: orders up to :data:`HOLDOUT_FRACTION` of the way
through the population's history train the model, and the rest is what it is
scored against. A random split would leak -- a visitor's later orders would
inform a model that is then asked to predict their earlier ones.

Four metrics come out of it, and the pairing is the point.

* :data:`HIT_RATE` -- of the visitors scored, the share for whom at least one of
  the :data:`TOP_K` recommendations turns up in their holdout orders.
* :data:`NOVEL_HIT_RATE` -- the same, counting only hits on items absent from
  that visitor's training history.

and then the same two numbers for a **popularity baseline**: recommend the
:data:`TOP_K` most-ordered items in the training window, to everybody.

The baseline is not a formality. PRD requirement P2 is that recommendations are
grounded in the visitor's actual ordering behaviour *rather than generic
popularity*, and adds that a global top-sellers list does not satisfy it **even
if it scores well**. It does score well: most people's next order contains a
staple, so popularity wins :data:`HIT_RATE` comfortably. What it cannot do is
recommend something a visitor has not already had. So the two numbers together
say what one cannot, and :func:`beats_baseline` is the promotion rule --
:data:`NOVEL_HIT_RATE` strictly above the baseline's, by at least
:data:`MINIMUM_MARGIN`. A run that cannot clear it logs its metrics and leaves
the alias where it is.

## Ties break on the data

Every ordering here breaks its ties on the identity of the thing chosen, for the
reason ``gold.py`` gives: an ordering that is reproducible only until the files
underneath are rewritten produces a visitor's recommendations changing for no
reason a month later. Candidates sort by ``(-score, item_id)``, seeds by
``(-weight, item_id)``, and :func:`recommend` is a pure function of its
arguments -- which is what lets ``test_recommender.py`` assert the property by
running it twice on shuffled input.

Scores are :class:`~decimal.Decimal` at the boundaries for the same reason
``gold.py``'s are, and the arithmetic in between is float: a shrunk lift is a
ratio of ratios and the last bits of a float division are not reproducible
across a partition change, so the value is quantized to :data:`SCORE`'s scale
before anything compares or ranks on it.

## What the registry gets

One registered model, :data:`MODEL_NAME`, in Unity Catalog -- three-level, so it
lives in ``gold_synthetic`` beside the marts it was trained from and is
governed by the same grants.

Each run logs a **version**. A run that clears :func:`beats_baseline` also moves
the :data:`CHAMPION_ALIAS` alias, and ``recommender_publish.py`` loads
``models:/<name>@champion`` and nothing else -- so a bad training run publishes
nothing rather than publishing worse recommendations.

**An alias, not a stage.** Model stages were the Workspace Model Registry's
mechanism and Unity Catalog replaced them with aliases; the ``Staging`` and
``Production`` stage transitions are not available on a UC model at all. The
issue asks for "a version and a stage", and :data:`CHAMPION_ALIAS` is what a
stage became. Writing ``transition_model_version_stage`` here in 2026 would be
writing against an API this registry does not have -- the same call the project
made about ``import dlt``.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = [
    "AGREEMENT",
    "BASELINE_HIT_RATE",
    "BASELINE_NOVEL_HIT_RATE",
    "CHAMPION_ALIAS",
    "COVERAGE",
    "DERIVED_AT",
    "ENTREE_CATEGORY",
    "EXPERIMENT",
    "FORBIDDEN_SOURCES",
    "HIT_RATE",
    "HOLDOUT_FRACTION",
    "LAYER",
    "MART",
    "MAX_RATIONALE_CHARS",
    "METRICS",
    "MINIMUM_CO_ORDERS",
    "MINIMUM_MARGIN",
    "MINIMUM_SCORE",
    "MODEL_COMMENT",
    "MODEL_NAME",
    "MODEL_SCHEMA",
    "NOVEL_HIT_RATE",
    "PAIRS_KEPT",
    "RATIONALE_JOIN",
    "RATIONALE_LEAD",
    "RATIONALE_TAIL",
    "RECOMMENDATIONS",
    "REFERENCE_MART",
    "SCORE",
    "SCORED_FIELDS",
    "SETTLED_STATUSES",
    "SHARE_PHRASES",
    "SHRINKAGE",
    "SOURCE_LAYER",
    "STREAM",
    "STREAMS",
    "TOP_K",
    "VISITORS_SCORED",
    "Affinity",
    "Column",
    "Expectation",
    "Hyperparameter",
    "Metric",
    "Phrase",
    "Recommendation",
    "Table",
    "affinity_query",
    "beats_baseline",
    "column_names",
    "expectations",
    "hit_rates",
    "hyperparameters",
    "metric",
    "phrase",
    "popular_items",
    "publish_query",
    "rationale",
    "rationale_expression",
    "recommend",
    "schema_name",
    "score",
    "scored_schema",
    "seed_weight",
    "takes_the_alias",
    "training_query",
]

# --- Where this sits in the medallion ----------------------------------------

LAYER: Final = "gold"
"""The medallion layer the published table and the registered model live in."""

SOURCE_LAYER: Final = "silver"
"""The layer the training queries read. Never bronze, for ``gold.py``'s reason."""

STREAMS: Final[tuple[str, ...]] = ("harvested", "synthetic")
"""The two populations, as plain strings.

``chip_chat.databricks.catalog.STREAMS`` is the definition and this is a copy,
for the reason the module docstring gives. ``test_recommender.py`` asserts they
agree.
"""

STREAM: Final = "synthetic"
"""The stream the model and its output are published into.

The model is fitted on generated orders and scores generated customers, so it is
synthetic however real the catalogue underneath it is -- the same call
``gold.STREAM`` makes about the four marts.
"""

MART: Final = "recommendations"
"""The table the registered model publishes, and the one the agent reads.

RFC-001 §06 backs ``get_recommendations`` with a gold mart, and this is it. It
is a fifth table in ``gold_synthetic`` rather than a change to one of §04's
four: ``item_affinity`` is three columns wide and carries no ``demo_id``, so
neither the visitor scoping nor the rationale this issue requires has anywhere
to go in it. ``docs/recommender.md`` §2 carries the argument in full.
"""

REFERENCE_MART: Final = "item_affinity"
"""The #36 mart the full-history refit has to reproduce.

Read by ``recommender_train.py`` for one purpose: :data:`AGREEMENT`. It is not
an input to the fit -- a fit needs order-level data it can split on time, and a
mart computed over all history cannot express a holdout -- but it is the
published statement of what a co-occurrence over this population looks like, so
a refit that disagrees with it is a finding.
"""

DERIVED_AT: Final = "derived_at"
"""When the row was computed. The one wall-clock value in the published table.

The same column, for the same RFC-001 §10 reason, as every mart in ``gold.py``
carries: a failed nightly job serves stale rows *with their timestamp*, never
silently as fresh.
"""

FORBIDDEN_SOURCES: Final[tuple[str, ...]] = ("demo_visitors",)
"""Tables this layer may not read, whatever it wants from them.

``gold.FORBIDDEN_SOURCES`` copied, asserted equal in the tests, and enforced
over these queries and these notebooks as well. The containment argument in
RFC-001 §04 -- no editable field is an input to a derived table -- is only worth
making if it holds over everything downstream of silver, and a recommender that
read a visitor's ``stated_preferences`` would break it while looking helpful.
"""

SETTLED_STATUSES: Final[tuple[str, ...]] = ("COMPLETED",)
"""The order statuses that count as evidence.

``gold.SETTLED_STATUSES`` copied, asserted equal in the tests. A cancelled order
never happened and a refunded one had its money returned; neither is a signal
about what somebody likes, and counting them here while ``item_affinity`` counts
them there would make :data:`AGREEMENT` fail for a reason that is not a model
problem.
"""

ENTREE_CATEGORY: Final = "Entree"
"""The ``menu_items.category`` that marks a composed entree.

``gold.ENTREE_CATEGORY`` copied, asserted equal in the tests. Used for one
thing: :func:`recommend` prefers a seed that is an entree when weights tie, so
the sentence reads *you order the barbacoa burrito* rather than *you order
chips*.
"""


# --- Hyperparameters ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hyperparameter:
    """One number the model was fitted with, and what moving it would do.

    MLflow logs a run's parameters as strings with no explanation attached, so
    what a parameter *means* has to live somewhere a person can read. This is
    that somewhere, and :func:`hyperparameters` is what the training notebook
    hands to ``mlflow.log_params`` -- so the run and the docstring cannot drift.

    Attributes:
        name: The parameter name, as MLflow records it.
        value: What it is.
        why: What the number does, and what changing it costs.
    """

    name: str
    value: object
    why: str


MINIMUM_CO_ORDERS: Final = 25
"""How many settled orders two items must share before the pair is scored.

A copy of ``gold.MINIMUM_CO_ORDERS``, asserted equal in the tests. Equal on
purpose rather than by coincidence: the model's full-history refit is required
to reproduce ``item_affinity``, and a refit that kept pairs the mart dropped
would disagree with it everywhere the two thresholds differ, which is a
difference in bookkeeping reported as a difference in the model.
"""

SHRINKAGE: Final = 40
"""Co-orders at which a pair keeps half the lift it claims.

Lift is a ratio and says nothing about how much evidence produced it: a pair
seen 25 times and a pair seen 2,500 times can carry the same number. The score
multiplies lift by ``co / (co + SHRINKAGE)``, so at ``co = SHRINKAGE`` a pair
scores half its lift and by a few hundred co-orders it scores nearly all of it.

Forty rather than twenty-five so that a pair which has only just cleared the
support floor is still visibly penalised; a shrinkage equal to the floor would
let the thinnest surviving pair keep 50% of its claim, which is more credit than
"we saw this 25 times" deserves. Raising it makes the model conservative and
crowds thin pairs out of :data:`TOP_K` entirely; lowering it toward zero turns
the score back into raw lift and puts the noisiest pairs at the top.
"""

MINIMUM_SCORE: Final = 1.0
"""The score below which a candidate is not recommended at all.

One is lift's own null value: a pair at exactly one is ordered together as often
as chance would have it, and below one is ordered together *less*. Recommending
from below the floor is worse than saying nothing, and because the score is a
shrunk lift, a thin pair has to carry a genuinely high lift to clear it.
"""

TOP_K: Final = 5
"""How many recommendations a visitor gets.

Enough that the agent has something to pick from when the conversation rules one
out, few enough that the tail is not padding. RFC-001 §06 returns *ranked*
items, so the consumer is free to use fewer; nothing here assumes all five are
shown.
"""

HOLDOUT_FRACTION: Final = 0.2
"""The share of the population's history, by time, kept back for evaluation.

Split on time and never at random. A random split would put a visitor's later
orders in the training set and ask the model to predict their earlier ones,
which is not a question anybody will ever ask it, and it would report a number
better than the one the deployed model earns.
"""

MINIMUM_MARGIN: Final = 0.01
"""How far above the popularity baseline a run must land to take the alias.

A percentage point of novel hit rate. Not zero: two numbers computed from the
same holdout differ in the last place for reasons that are not improvements, and
an alias that moves on noise is an alias that means nothing. See
:func:`beats_baseline`.
"""


def hyperparameters() -> tuple[Hyperparameter, ...]:
    """Return every fitted parameter, in the order the notebook logs them.

    Returns:
        The parameters, each with the sentence that says what it does.
    """
    return (
        Hyperparameter(
            name="minimum_co_orders",
            value=MINIMUM_CO_ORDERS,
            why=(
                "support floor: a pair seen fewer times than this is not scored "
                "at all. Equal to gold.MINIMUM_CO_ORDERS so the full-history "
                "refit can be compared to the published item_affinity mart"
            ),
        ),
        Hyperparameter(
            name="shrinkage",
            value=SHRINKAGE,
            why=(
                "co-orders at which a pair keeps half its lift. Shrinks a thin "
                "pair toward independence, which raw lift has no way to express"
            ),
        ),
        Hyperparameter(
            name="minimum_score",
            value=MINIMUM_SCORE,
            why=(
                "lift's null value. Below it two items are ordered instead of "
                "each other, which is a reason to say nothing"
            ),
        ),
        Hyperparameter(
            name="top_k",
            value=TOP_K,
            why="recommendations per visitor, ranked",
        ),
        Hyperparameter(
            name="holdout_fraction",
            value=HOLDOUT_FRACTION,
            why=(
                "share of the history, by time, held back for evaluation. Split "
                "on time so a visitor's later orders cannot inform a prediction "
                "about their earlier ones"
            ),
        ),
        Hyperparameter(
            name="minimum_margin",
            value=MINIMUM_MARGIN,
            why=(
                "novel hit rate a run must beat the popularity baseline by "
                "before it takes the champion alias"
            ),
        ),
        Hyperparameter(
            name="excludes_everything_tried",
            value=True,
            why=(
                "every item in the visitor's own settled history is excluded, "
                "not merely the ones they order constantly. The strong rule "
                "makes 'things they have genuinely not tried' an emptiness "
                "assertion rather than an argument about a threshold"
            ),
        ),
    )


# --- Metrics ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metric:
    """One number a training run reports, and how to read it.

    Attributes:
        name: The metric name, as MLflow records it.
        summary: What it measures, in one line.
        why: What it is for -- which claim it supports or refutes. The
            interesting half for the two hit rates, which are only meaningful
            as a pair.
    """

    name: str
    summary: str
    why: str


HIT_RATE: Final = Metric(
    name="hit_rate_at_k",
    summary=(
        "share of scored visitors for whom at least one of the top-k "
        "recommendations appears in their holdout orders"
    ),
    why=(
        "the obvious measure, and on its own the misleading one. It rewards "
        "recommending what somebody was going to order anyway, which is why the "
        "popularity baseline beats the model on it"
    ),
)

NOVEL_HIT_RATE: Final = Metric(
    name="novel_hit_rate_at_k",
    summary=(
        "the same share, counting only hits on items absent from that visitor's "
        "training history"
    ),
    why=(
        "PRD requirement P2: recommendations grounded in the visitor's actual "
        "ordering behaviour rather than generic popularity. This is the number "
        "a top-sellers list cannot win, and the one the alias moves on"
    ),
)

BASELINE_HIT_RATE: Final = Metric(
    name="baseline_hit_rate_at_k",
    summary=f"{HIT_RATE.summary}, for the most-ordered items in the training window",
    why=(
        "the control. Expected to beat the model, and the reason the issue's "
        "requirement is phrased about behaviour rather than about accuracy"
    ),
)

BASELINE_NOVEL_HIT_RATE: Final = Metric(
    name="baseline_novel_hit_rate_at_k",
    summary=f"{NOVEL_HIT_RATE.summary}, for the same top-sellers list",
    why=(
        "the comparison that decides whether a run is an improvement. A "
        "top-sellers list can only be novel to somebody who has not yet tried "
        "the top sellers, which is not many people"
    ),
)

COVERAGE: Final = Metric(
    name="catalogue_coverage",
    summary="share of orderable items that appear in at least one visitor's top-k",
    why=(
        "a model that recommends the same six items to everybody can score well "
        "on both hit rates. Coverage is what says so, and it is reported rather "
        "than gated -- a real menu has items nobody should be pushed toward"
    ),
)

AGREEMENT: Final = Metric(
    name="item_affinity_agreement",
    summary=(
        "share of pairs where the full-history refit's lift equals the "
        "published item_affinity mart's, to the mart's own scale"
    ),
    why=(
        "issue #37 asks that the mart come from the model. #36 landed first and "
        "made it a materialized view whose determinism is its own acceptance "
        "criterion, so the relationship is inverted: the refit must reproduce "
        "the mart. Anything below 1.0 means the two definitions have drifted"
    ),
)

VISITORS_SCORED: Final = Metric(
    name="visitors_scored",
    summary="visitors the holdout evaluation could score at all",
    why=(
        "a hit rate over four visitors is not a measurement. Logged beside the "
        "rates so the denominator is never implicit"
    ),
)

PAIRS_KEPT: Final = Metric(
    name="pairs_kept",
    summary=f"ordered item pairs surviving the {MINIMUM_CO_ORDERS}-co-order floor",
    why=(
        "the model's whole state. A support threshold that silently drops most "
        "of the table reads, from the outside, exactly like a population with "
        "no affinities in it"
    ),
)

METRICS: Final[tuple[Metric, ...]] = (
    HIT_RATE,
    NOVEL_HIT_RATE,
    BASELINE_HIT_RATE,
    BASELINE_NOVEL_HIT_RATE,
    COVERAGE,
    AGREEMENT,
    VISITORS_SCORED,
    PAIRS_KEPT,
)
"""Every metric a training run logs, in the order the notebook reports them."""


def metric(name: str) -> Metric:
    """Return the metric called ``name``.

    Args:
        name: A metric name as MLflow records it.

    Returns:
        The metric.

    Raises:
        KeyError: If no metric has that name.
    """
    for candidate in METRICS:
        if candidate.name == name:
            return candidate
    raise KeyError(f"no metric is called {name!r}")


# --- The registry -------------------------------------------------------------

MODEL_SCHEMA: Final = f"{LAYER}_{STREAM}"
"""The Unity Catalog schema holding the registered model: ``gold_synthetic``.

The model lives beside the marts it was trained from rather than in a schema of
its own, so that the grants in ``databricks_catalog.tf`` already reach it and a
principal who may not read the synthetic population may not load the model
fitted on it either.
"""

MODEL_NAME: Final = "item_affinity_recommender"
"""The registered model, unqualified.

``recommender_train.py`` qualifies it as ``<catalog>.gold_synthetic.<name>``:
Unity Catalog model names are three-level, which is the whole difference between
this registry and the workspace one -- the model is a securable in the same
namespace as the tables, not an entry in a side registry with its own
permissions model.
"""

CHAMPION_ALIAS: Final = "champion"
"""The alias ``recommender_publish.py`` loads, and the only one that is moved.

What a stage became. Unity Catalog replaced the Workspace Model Registry's
``Staging``/``Production`` transitions with aliases, so the "stage" #37 asks for
is this, and a run that cannot clear :func:`beats_baseline` leaves it pointing
at the previous version. That is the property worth having: a bad training run
publishes nothing rather than publishing something worse.
"""

EXPERIMENT: Final = "item-affinity-recommender"
"""The MLflow experiment every run is tracked in, as a name rather than a path.

Terraform makes the experiment and passes its path in; a notebook that fell back
to its own default experiment would log runs into a workspace path named after
the notebook, where nobody comparing versions would look for them.
"""

MODEL_COMMENT: Final = (
    "Item-affinity co-occurrence recommender (gh-37). Scores a visitor's "
    "unordered items by the shrunk lift of their strongest ordered item against "
    "each candidate, excludes everything they have ever ordered, and returns "
    "the top few with the seed that earned each one. Fitted on "
    "silver_synthetic.orders and order_items; the full-history refit reproduces "
    "gold_synthetic.item_affinity. The @champion alias only moves when a run "
    "beats a popularity baseline on NOVEL hit rate, because PRD P2 is explicit "
    "that a top-sellers list does not satisfy the requirement even when it "
    "scores well. Batch-scored into gold_synthetic.recommendations; nothing "
    "calls this model on the conversational path."
)
"""The Unity Catalog comment on the registered model.

Long, because a model in a catalogue browser is a name and a version number
unless somebody wrote down what it does and what it refuses to do.
"""


# --- The rationale ------------------------------------------------------------

MAX_RATIONALE_CHARS: Final = 160
"""How long a rationale may be.

The issue asks for a *short* rationale the agent can surface, and short is only
enforceable as a number. A hundred and sixty characters is a sentence the
assistant can say without reformatting and short enough that it cannot become a
paragraph of hedging. Enforced as a mart expectation, not merely as a test, so a
menu item with a very long published name fails the update rather than shipping
a rationale the agent truncates mid-word.
"""


@dataclass(frozen=True, slots=True)
class Phrase:
    """How often a visitor orders something, in words.

    Attributes:
        floor: The lowest share this phrase covers, inclusive.
        words: What to say. Reads directly after the item name.
    """

    floor: Decimal
    words: str


SHARE_PHRASES: Final[tuple[Phrase, ...]] = (
    Phrase(floor=Decimal("0.50"), words="in most of your orders"),
    Phrase(floor=Decimal("0.25"), words="pretty regularly"),
    Phrase(floor=Decimal("0"), words="now and then"),
)
"""What a seed's share of a visitor's orders sounds like, highest floor first.

The same device as ``gold.CONFIDENCE_BANDS`` and for the same reason: a number
in a sentence the assistant says out loud is a number the visitor has to
interpret, and *you order the barbacoa burrito in 43% of your orders* is a
sentence no person has ever said. The bands are what turns a proportion into
something a human would say about themselves.

Three, not more. Each band has to be a phrase somebody would actually use, and
the fourth one is always a synonym of the third.
"""

RATIONALE_LEAD: Final = "You order "
"""How a rationale opens. Second person, present tense, no hedging.

The visitor's own behaviour is the evidence, so the sentence starts with it.
"""

RATIONALE_JOIN: Final = ", and people who do tend to add "
"""What sits between the seed and the recommendation.

"People who do" is the co-occurrence claim, stated as what it is: a fact about
the population, not a claim about the visitor. "Tend to" is the shrinkage
showing up in the prose -- the model is reporting a tendency, and a sentence
that said *you will like* would be claiming something it has not measured.
"""

RATIONALE_TAIL: Final = "."
"""How a rationale closes. One sentence, ended."""


def phrase(share: Decimal | float | int) -> Phrase:
    """Return how ``share`` sounds in words.

    Args:
        share: A seed's share of a visitor's settled orders, in ``[0, 1]``.

    Returns:
        The highest phrase whose floor ``share`` reaches.

    Raises:
        ValueError: If ``share`` is outside ``[0, 1]``, which is not a share
            anything here could have produced.
    """
    reading = Decimal(str(share))
    if not Decimal(0) <= reading <= Decimal(1):
        raise ValueError(f"a share is in [0, 1]; got {share!r}")
    for candidate in SHARE_PHRASES:
        if reading >= candidate.floor:
            return candidate
    raise AssertionError("the last phrase has a floor of zero and cannot be missed")


def rationale(seed_name: str, share: Decimal | float | int, item_name: str) -> str:
    """Return the sentence explaining one recommendation.

    The Python half of a definition whose SQL half is
    :func:`rationale_expression`. Neither is the authority: the published
    rationale is rendered in SQL, because the item *names* live in
    ``silver_harvested.menu_items`` and joining them is a join, and this is what
    ``test_recommender.py`` holds that SQL to.

    Args:
        seed_name: The published name of the item the visitor already orders.
        share: That item's share of their settled orders.
        item_name: The published name of the item being recommended.

    Returns:
        One sentence, of the shape the issue asks for: *You order the Barbacoa
        Burrito in most of your orders, and people who do tend to add the
        Tomatillo Red-Chili Salsa.*

    Raises:
        ValueError: If either name is empty -- a rationale naming nothing is a
            sentence with a hole in it -- or if ``share`` is not a share.
    """
    if not seed_name or not item_name:
        raise ValueError("a rationale names two items; got an empty name")
    return (
        f"{RATIONALE_LEAD}{seed_name} {phrase(share).words}"
        f"{RATIONALE_JOIN}{item_name}{RATIONALE_TAIL}"
    )


def rationale_expression(seed_name: str, share: str, item_name: str) -> str:
    """Return the SQL that renders :func:`rationale` over three columns.

    Args:
        seed_name: The column holding the seed item's published name.
        share: The column holding the seed's share of the visitor's orders.
        item_name: The column holding the recommended item's published name.

    Returns:
        A SQL string expression.
    """
    cases = " ".join(
        f"WHEN {share} >= {candidate.floor} THEN '{candidate.words}'"
        for candidate in SHARE_PHRASES
    )
    return (
        f"concat('{RATIONALE_LEAD}', {seed_name}, ' ', "
        f"CASE {cases} END, "
        f"'{RATIONALE_JOIN}', {item_name}, '{RATIONALE_TAIL}')"
    )


# --- The model ----------------------------------------------------------------

SCORE: Final = "DECIMAL(12,6)"
"""What a recommendation's score becomes.

The same type and scale as ``gold.LIFT``, because a score is a lift with a
shrinkage factor applied and a reader comparing the two should not have to
account for a difference in precision. Exact rather than double for that module's
reason: a value that depends on the order Spark added things up in is a value a
rebuild does not reproduce.
"""

_SCALE: Final = Decimal("0.000001")
""":data:`SCORE`'s scale, as a quantum. Six places."""


@dataclass(frozen=True, slots=True)
class Affinity:
    """One ordered pair of items, as the fitted model holds it.

    This is the model's entire state. It is a co-occurrence count and three
    denominators rather than a score, so that :func:`score` can be re-derived
    with different hyperparameters from the same fit -- and so that a person
    reading a logged artifact can see the evidence rather than only the verdict.

    Attributes:
        item_id: The item the visitor already orders.
        related_item_id: The item that turns up with it. Never the same item.
        co_orders: Settled orders containing both.
        orders_with_item: Settled orders containing ``item_id``.
        orders_with_related: Settled orders containing ``related_item_id``.
        orders: Settled orders in the fitting window, in total.
    """

    item_id: str
    related_item_id: str
    co_orders: int
    orders_with_item: int
    orders_with_related: int
    orders: int

    @property
    def lift(self) -> Decimal:
        """``P(both) / (P(item) · P(related))``, as ``gold.item_affinity`` defines it.

        Four integers and one division, evaluated in exact arithmetic -- the
        same expression the mart's SQL evaluates, which is what makes
        :data:`AGREEMENT` a check of the model rather than of the arithmetic.
        """
        denominator = Decimal(self.orders_with_item) * Decimal(self.orders_with_related)
        if denominator == 0:
            raise ValueError(
                f"{self.item_id}/{self.related_item_id} claims "
                f"{self.co_orders} co-orders but one of the two items appears "
                "in no order at all"
            )
        exact = Decimal(self.co_orders) * Decimal(self.orders) / denominator
        return exact.quantize(_SCALE, rounding=ROUND_HALF_UP)

    def as_row(self) -> dict[str, object]:
        """Return the pair as the logged artifact holds it: field name to value.

        The keys are :class:`Affinity`'s own field names, because
        ``recommender_model.Recommender.load_context`` reads the artifact back
        with ``Affinity(**row)`` -- the round trip is the artifact's whole
        contract and a renamed key would break it at model load rather than
        here.

        This exists as a method rather than a ``vars()`` at the call site, and
        the training run is where that mattered: this dataclass is
        ``slots=True``, so it has no ``__dict__`` and ``vars()`` raises
        ``TypeError: vars() argument must have __dict__ attribute`` -- after the
        fit, after both hit-rate evaluations, at the point of logging the model.
        Six minutes of cluster time to find out that a builtin does not apply.
        """
        return {field: getattr(self, field) for field in self.__slots__}


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One suggestion for one visitor.

    Attributes:
        item_id: What to suggest. Never something the visitor has ordered.
        seed_item_id: The item of theirs that earned it -- the argmax, and the
            item :func:`rationale` names.
        seed_share: That item's share of their settled orders, which is what
            :func:`phrase` turns into words.
        score: The shrunk lift, weighted by the seed's share.
        rank: 1 for the strongest. Dense, contiguous, and 1-based, because it
            is rendered to a person rather than indexed by a program.
    """

    item_id: str
    seed_item_id: str
    seed_share: Decimal
    score: Decimal
    rank: int


def score(pair: Affinity, shrinkage: int = SHRINKAGE) -> Decimal:
    """Return the shrunk lift of ``pair``.

    ``lift · co / (co + shrinkage)``. See "What a score is" in the module
    docstring for why lift alone is not enough.

    Args:
        pair: The fitted co-occurrence.
        shrinkage: Co-orders at which a pair keeps half its lift. Defaults to
            :data:`SHRINKAGE`; taken as an argument so a training run can sweep
            it without this module holding a mutable global.

    Returns:
        The score, quantized to :data:`SCORE`'s scale.

    Raises:
        ValueError: If ``shrinkage`` is negative, which would inflate a thin
            pair rather than shrink it.
    """
    if shrinkage < 0:
        raise ValueError(f"shrinkage discounts thin pairs; got {shrinkage!r}")
    weight = Decimal(pair.co_orders) / Decimal(pair.co_orders + shrinkage)
    return (pair.lift * weight).quantize(_SCALE, rounding=ROUND_HALF_UP)


def seed_weight(orders_with_item: int, orders: int) -> Decimal:
    """Return the share of a visitor's settled orders that contained an item.

    Args:
        orders_with_item: How many of their orders contained it.
        orders: How many settled orders they placed. At least one.

    Returns:
        The share, quantized to :data:`SCORE`'s scale.

    Raises:
        ValueError: If ``orders`` is not positive, or ``orders_with_item`` is
            negative or exceeds it. Each is a caller that has lost track of what
            it is counting.
    """
    if orders < 1:
        raise ValueError(f"a share needs at least one order; got {orders!r}")
    if not 0 <= orders_with_item <= orders:
        raise ValueError(f"{orders_with_item!r} of {orders!r} orders is not a share")
    exact = Decimal(orders_with_item) / Decimal(orders)
    return exact.quantize(_SCALE, rounding=ROUND_HALF_UP)


def recommend(
    history: Mapping[str, int],
    orders: int,
    affinities: Iterable[Affinity],
    *,
    entrees: frozenset[str] = frozenset(),
    top_k: int = TOP_K,
    minimum_score: float = MINIMUM_SCORE,
    shrinkage: int = SHRINKAGE,
) -> tuple[Recommendation, ...]:
    """Return one visitor's recommendations, strongest first.

    The whole of the model's scoring, and a pure function of its arguments --
    which is what lets ``test_recommender.py`` assert determinism by running it
    twice over shuffled input, and what lets the same code run under pytest, on
    a Spark driver and inside a served MLflow model.

    Args:
        history: How many of the visitor's settled orders contained each item.
            Its keys are the exclusion list as well as the seeds: nothing in it
            can be recommended. See "What it refuses to say".
        orders: How many settled orders the visitor placed.
        affinities: The fitted pairs. Only those whose ``item_id`` the visitor
            orders are looked at.
        entrees: Item ids that are composed entrees, used only to break a tie
            between two seeds of equal weight -- naming an entree makes a better
            sentence than naming a bag of chips. Empty means "do not prefer".
        top_k: How many to return.
        minimum_score: The floor a *pair* must clear, before the visitor's own
            share weights it.
        shrinkage: Passed through to :func:`score`.

    Returns:
        Up to ``top_k`` recommendations, ranked from 1. Empty for a visitor with
        no history, or none whose items have a surviving pair -- an honest
        absence rather than a popularity fallback.

    Raises:
        ValueError: If ``top_k`` is not positive, or ``history`` claims more
            orders of an item than the visitor placed.
    """
    if top_k < 1:
        raise ValueError(f"a visitor gets at least one recommendation; got {top_k!r}")
    if not history:
        return ()

    weights = {
        item: seed_weight(count, orders)
        for item, count in sorted(history.items())
        if count > 0
    }
    if not weights:
        return ()

    best: dict[str, tuple[Decimal, str]] = {}
    floor = Decimal(str(minimum_score))
    for pair in affinities:
        if pair.item_id not in weights or pair.related_item_id in history:
            continue
        if pair.co_orders < MINIMUM_CO_ORDERS:
            continue
        # The floor is a statement about the pair, so it is applied before the
        # visitor's share weights it: "these two items do not go together" is a
        # reason to stay silent and "this visitor does not order the first one
        # very often" is not.
        strength = score(pair, shrinkage)
        if strength < floor:
            continue
        weighted = (weights[pair.item_id] * strength).quantize(
            _SCALE, rounding=ROUND_HALF_UP
        )
        # Ties break on the seed, and an entree beats a side at equal weight so
        # that the sentence names the thing the visitor thinks of as their
        # order. Both halves are properties of the seed, never of arrival order.
        contender = (weighted, pair.item_id)
        held = best.get(pair.related_item_id)
        if held is None or _outranks(contender, held, entrees):
            best[pair.related_item_id] = contender

    ranked = sorted(best.items(), key=lambda entry: (-entry[1][0], entry[0]))
    return tuple(
        Recommendation(
            item_id=item,
            seed_item_id=seed,
            seed_share=weights[seed],
            score=strength,
            rank=position,
        )
        for position, (item, (strength, seed)) in enumerate(ranked[:top_k], start=1)
    )


def _outranks(
    contender: tuple[Decimal, str],
    held: tuple[Decimal, str],
    entrees: frozenset[str],
) -> bool:
    """Return whether ``contender`` should replace ``held`` as a candidate's seed.

    Higher score wins. At equal score an entree beats a non-entree, and then the
    lower item id wins -- so the answer depends on the identity of the seeds and
    never on the order the pairs arrived in.
    """
    if contender[0] != held[0]:
        return contender[0] > held[0]
    contender_is_entree = contender[1] in entrees
    held_is_entree = held[1] in entrees
    if contender_is_entree != held_is_entree:
        return contender_is_entree
    return contender[1] < held[1]


SCORED_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("item_id", "string"),
    ("seed_item_id", "string"),
    ("seed_share", "string"),
    ("score", "string"),
    ("rank", "int"),
)
"""The model's per-suggestion output, as (field, Spark type) pairs.

:class:`Recommendation`'s fields, in its declaration order, which
``test_recommender.py`` asserts. The two decimals cross as **strings** and that
is the whole reason this is written down rather than inlined into the publish
notebook: JSON has one number type and it is a double, so a decimal that
travelled as a JSON number would come back with a rounding error in the last
place and :data:`SCORE`'s exactness -- which exists so a rebuild reproduces the
row -- would be a claim the format had already broken.
"""


def scored_schema() -> str:
    """Return the Spark type of the model's JSON output, for ``from_json``.

    Returns:
        An ``array<struct<...>>`` type string over :data:`SCORED_FIELDS`.
    """
    fields = ",".join(f"{name}:{sql_type}" for name, sql_type in SCORED_FIELDS)
    return f"array<struct<{fields}>>"


def popular_items(counts: Mapping[str, int], top_k: int = TOP_K) -> tuple[str, ...]:
    """Return the most-ordered items, strongest first. The baseline's whole model.

    Args:
        counts: How many settled orders in the training window contained each
            item.
        top_k: How many to return.

    Returns:
        Up to ``top_k`` item ids, ties broken on the item id.

    Raises:
        ValueError: If ``top_k`` is not positive.
    """
    if top_k < 1:
        raise ValueError(f"a baseline recommends at least one item; got {top_k!r}")
    ranked = sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))
    return tuple(item for item, count in ranked[:top_k] if count > 0)


def hit_rates(
    suggested: Mapping[str, Sequence[str]],
    holdout: Mapping[str, Iterable[str]],
    tried: Mapping[str, Iterable[str]],
) -> tuple[Decimal, Decimal, int]:
    """Return the plain hit rate, the novel hit rate, and the denominator.

    A visitor counts as a hit if any suggested item turns up in their holdout
    orders, and as a *novel* hit only if that item is absent from their training
    history. The two are computed here rather than in the notebook so that the
    model and the popularity baseline are scored by identical code -- a
    comparison where the two sides were measured by two functions is a
    comparison of the functions.

    Args:
        suggested: What each visitor was recommended.
        holdout: What each visitor actually ordered after the split.
        tried: What each visitor had ordered before it.

    Returns:
        ``(hit_rate, novel_hit_rate, visitors)``, the rates quantized to
        :data:`SCORE`'s scale. Both rates are zero when no visitor could be
        scored, which is a fact the caller should log rather than divide by.
    """
    scored = [visitor for visitor in sorted(suggested) if visitor in holdout]
    if not scored:
        return Decimal(0), Decimal(0), 0
    hits = 0
    novel_hits = 0
    for visitor in scored:
        ordered = set(holdout[visitor])
        before = set(tried.get(visitor, ()))
        landed = [item for item in suggested[visitor] if item in ordered]
        if landed:
            hits += 1
        if any(item not in before for item in landed):
            novel_hits += 1
    total = Decimal(len(scored))
    return (
        (Decimal(hits) / total).quantize(_SCALE, rounding=ROUND_HALF_UP),
        (Decimal(novel_hits) / total).quantize(_SCALE, rounding=ROUND_HALF_UP),
        len(scored),
    )


def beats_baseline(
    novel_hit_rate: Decimal | float,
    baseline_novel_hit_rate: Decimal | float,
    margin: float = MINIMUM_MARGIN,
) -> bool:
    """Return whether a run has earned the :data:`CHAMPION_ALIAS` alias.

    The promotion rule, and the only place it is written down. It is about
    :data:`NOVEL_HIT_RATE` alone: a model that beat the popularity baseline on
    :data:`HIT_RATE` and lost on novelty would be a popularity list with extra
    steps, which is exactly what PRD requirement P2 rules out.

    Args:
        novel_hit_rate: This run's.
        baseline_novel_hit_rate: The popularity baseline's, over the same
            holdout.
        margin: How far above the baseline is far enough. Defaults to
            :data:`MINIMUM_MARGIN`.

    Returns:
        True if the run should take the alias.

    Raises:
        ValueError: If ``margin`` is negative, which would promote a run that
            lost.
    """
    if margin < 0:
        raise ValueError(f"a margin cannot be negative; got {margin!r}")
    return Decimal(str(novel_hit_rate)) - Decimal(str(baseline_novel_hit_rate)) >= (
        Decimal(str(margin))
    )


def takes_the_alias(
    novel_hit_rate: Decimal | float,
    baseline_novel_hit_rate: Decimal | float,
    *,
    has_champion: bool,
    margin: float = MINIMUM_MARGIN,
) -> bool:
    """Return whether this run should move :data:`CHAMPION_ALIAS`.

    :func:`beats_baseline` is the rule and this is the rule plus the one case
    the rule does not cover: **there is no champion yet.**

    The gate exists to stop a run *replacing* a good incumbent with something
    that is a popularity list with extra steps. With no incumbent there is
    nothing to protect, and what the gate would protect instead is an empty
    serving table -- `recommender_publish.py` loads `@champion` and nothing
    else, so a first run that does not clear the margin leaves the registry
    holding a version, the alias unset, and the publish task failing with
    `RESOURCE_DOES_NOT_EXIST: Registered Model Alias 'champion' does not
    exist`. That is what happened on the first live run, and the message names
    a missing alias rather than the situation.

    So the first version takes the alias on the strength of being the only one,
    and its metrics say plainly whether it beat the baseline. Every version
    after it is held to the margin. `docs/recommender.md` §6 records what the
    first live run's metrics actually were, which is the point of doing it this
    way round rather than lowering the margin until something passed.

    Args:
        novel_hit_rate: This run's.
        baseline_novel_hit_rate: The popularity baseline's, same holdout.
        has_champion: Whether :data:`CHAMPION_ALIAS` currently resolves to a
            version. Read off the registry by the caller, because this module
            may not import MLflow.
        margin: How far above the baseline is far enough.

    Returns:
        Whether to move the alias to this run's version.
    """
    if not has_champion:
        return True
    return beats_baseline(novel_hit_rate, baseline_novel_hit_rate, margin)


# --- The published table ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    """One column of :data:`MART`.

    Attributes:
        name: The column name. The agent's read tool queries these by name.
        sql_type: What it is, as the published table declares it.
        why: What the value means. This is the column's definition and there is
            nowhere else it is written down.
    """

    name: str
    sql_type: str
    why: str


@dataclass(frozen=True, slots=True)
class Expectation:
    """One constraint every published row must satisfy.

    No action field, for ``gold.Expectation``'s reason: this table is what the
    agent answers from, so a row that violates its own definition is a wrong
    answer in a conversation rather than a warning in an event log. The publish
    notebook applies every one of these as a filter that must match nothing and
    fails the run if any does.

    Attributes:
        name: A statement of what is true, rather than of what went wrong.
        constraint: A SQL boolean expression over the table's own columns.
        why: Why this is worth failing a job for.
    """

    name: str
    constraint: str
    why: str


@dataclass(frozen=True, slots=True)
class Table:
    """The published table, as data.

    Attributes:
        name: The gold table name, unqualified.
        stream: The stream it is published into.
        grain: What one row is, in words. The first thing to read.
        columns: Every column, in the order the query produces them.
        required: Columns that may not be null, which become expectations.
        comment: What the table holds, for the Unity Catalog comment.
        expectations: Constraints beyond the ones derived from ``required``.
    """

    name: str
    stream: str
    grain: str
    columns: tuple[Column, ...]
    comment: str
    required: tuple[str, ...] = ()
    expectations: tuple[Expectation, ...] = ()


RECOMMENDATIONS: Final = Table(
    name=MART,
    stream=STREAM,
    grain="one row per visitor per recommendation, at most TOP_K per visitor",
    columns=(
        Column(
            name="demo_id",
            sql_type="STRING",
            why=(
                "whose recommendations these are. What #43's row access policy "
                "compares, and the reason this is a table of its own rather "
                "than columns on item_affinity, which is population-scoped"
            ),
        ),
        Column(
            name="rank",
            sql_type="INT",
            why=(
                "1 for the strongest. Dense and contiguous within a visitor, "
                "because RFC-001 §06 returns ranked items and the rank is "
                "rendered rather than indexed"
            ),
        ),
        Column(
            name="item_id",
            sql_type="STRING",
            why=(
                "what to suggest. Guaranteed absent from this visitor's own "
                "settled order history -- the exclusion is the whole of the "
                "issue's second acceptance criterion and is asserted as an "
                "emptiness join in recommender_verify.py"
            ),
        ),
        Column(
            name="seed_item_id",
            sql_type="STRING",
            why=(
                "the item of theirs that earned it: the argmax over their own "
                "orders, and the item the rationale names. Present as a column "
                "so the sentence can be re-derived and disputed rather than "
                "only read"
            ),
        ),
        Column(
            name="seed_share",
            sql_type=SCORE,
            why=(
                "the share of this visitor's settled orders that contained the "
                "seed. The number SHARE_PHRASES turns into the words in the "
                "rationale, published so the sentence can be checked against "
                "the evidence -- and so the independence floor stays checkable "
                "in the table, because score divided by this is the pair's own "
                "shrunk lift"
            ),
        ),
        Column(
            name="score",
            sql_type=SCORE,
            why=(
                "the seed's share of their orders times the shrunk lift of the "
                "pair. Comparable within a visitor; across visitors it is "
                "scaled by how concentrated their ordering is, so it ranks "
                "rather than measures"
            ),
        ),
        Column(
            name="rationale",
            sql_type="STRING",
            why=(
                "the one sentence the agent surfaces, at most "
                f"{MAX_RATIONALE_CHARS} characters. Rendered from the seed's "
                "published name, how often they order it in words, and the "
                "recommended item's name"
            ),
        ),
        Column(
            name="model_version",
            sql_type="STRING",
            why=(
                "the Unity Catalog model version that produced the row. What "
                "makes a recommendation traceable to a run, its parameters and "
                "its metrics rather than to 'the recommender'"
            ),
        ),
        Column(
            name=DERIVED_AT,
            sql_type="TIMESTAMP",
            why=(
                "when the row was computed. RFC-001 §10 serves a stale table "
                "with its timestamp, never silently as fresh"
            ),
        ),
    ),
    required=(
        "demo_id",
        "rank",
        "item_id",
        "seed_item_id",
        "seed_share",
        "score",
        "rationale",
        "model_version",
        DERIVED_AT,
    ),
    expectations=(
        Expectation(
            name="recommends_something_else",
            constraint="item_id <> seed_item_id",
            why=(
                "a recommendation seeded by itself is the exclusion rule having "
                "failed, and it would read to a visitor as being told to order "
                "what they just ordered"
            ),
        ),
        Expectation(
            name="ranks_within_the_top_k",
            constraint=f"rank >= 1 AND rank <= {TOP_K}",
            why=(
                "the rank is rendered to a person. A sixth row in a top-five is "
                "a limit that stopped being applied somewhere upstream"
            ),
        ),
        Expectation(
            name="is_a_share_of_the_visitors_orders",
            constraint="seed_share > 0 AND seed_share <= 1",
            why=(
                "SHARE_PHRASES reads it as a proportion, and a value outside "
                "the range would land in a phrase by accident rather than by "
                "measurement. Zero would mean the seed came from a visitor who "
                "never ordered it"
            ),
        ),
        Expectation(
            name="clears_independence",
            constraint=f"score >= {MINIMUM_SCORE} * seed_share",
            why=(
                "score is seed_share times the pair's shrunk lift, so this says "
                "the lift itself cleared lift's null value. Below it two items "
                "are ordered instead of each other and the row is a suggestion "
                "made against the evidence -- and stating it this way keeps the "
                "floor about the pair rather than about how often the visitor "
                "orders the seed"
            ),
        ),
        Expectation(
            name="explains_itself_briefly",
            constraint=(
                f"length(rationale) > 0 AND length(rationale) <= {MAX_RATIONALE_CHARS}"
            ),
            why=(
                "the issue's third acceptance criterion is a short rationale "
                "the agent can surface, and short is only enforceable as a "
                "number. An empty one is a row the agent would have to explain "
                "on its own, which is where a confident invention comes from"
            ),
        ),
    ),
    comment=(
        "Ranked item recommendations per visitor with the sentence explaining "
        "each one, batch-scored from the @champion version of "
        "item_affinity_recommender (gh-37). Nothing calls a model on the "
        "conversational path; RFC-001 §06's get_recommendations reads this "
        "table. Every row is for an item the visitor has NEVER ordered, and a "
        "visitor with too little history has no rows rather than a popularity "
        "fallback -- PRD P2 is explicit that a top-sellers list does not "
        "satisfy the requirement even when it scores well. score is "
        "seed_share * shrunk lift and ranks within a visitor rather than "
        "across them. model_version says which registered version said so."
    ),
)
"""The one table this issue publishes."""


def schema_name(stream: str) -> str:
    """Return the unqualified gold schema for ``stream``.

    Args:
        stream: One of :data:`STREAMS`.

    Returns:
        ``gold_harvested`` or ``gold_synthetic``.

    Raises:
        ValueError: If ``stream`` is not one of :data:`STREAMS`.
    """
    if stream not in STREAMS:
        raise ValueError(f"unknown stream {stream!r}; expected one of {STREAMS}")
    return f"{LAYER}_{stream}"


def column_names() -> tuple[str, ...]:
    """Return :data:`RECOMMENDATIONS`' column names, in order."""
    return tuple(column.name for column in RECOMMENDATIONS.columns)


def expectations() -> tuple[Expectation, ...]:
    """Return every constraint applied to the published table.

    Two sources, in this order: one per required column, then the ones the
    table declares for itself. All of them are checked the same way -- see
    :class:`Expectation`.

    Returns:
        The expectations, with unique names.

    Raises:
        ValueError: If two end up sharing a name, which would leave one of them
            silently unreported.
    """
    derived = [
        Expectation(
            name=f"{column}_is_present",
            constraint=f"{column} IS NOT NULL",
            why=(
                f"{column} identifies, scopes or explains the row, and a null "
                "there is a row the serving layer cannot answer from"
            ),
        )
        for column in RECOMMENDATIONS.required
    ]
    derived += list(RECOMMENDATIONS.expectations)
    names = [item.name for item in derived]
    if len(names) != len(set(names)):
        raise ValueError(
            f"{RECOMMENDATIONS.name} declares two expectations with one name: "
            f"{sorted(name for name in names if names.count(name) > 1)}"
        )
    return tuple(derived)


# --- The queries --------------------------------------------------------------
#
# Three, and each is a template with `{placeholder}` names for every table and
# every threshold, filled by the function beside it. The reason is `gold.py`'s:
# a threshold cannot drift from the SQL that applies it when there is exactly
# one of each, and the verify notebook can re-run the training query the run
# actually ran.

_SETTLED = """settled AS (
        SELECT order_id, demo_id, placed_at
        FROM {orders}
        WHERE status IN ({settled})
    )"""

_TRAINING = (
    """WITH """
    + _SETTLED
    + """,
    -- The split instant, read out of the data rather than off the wall clock,
    -- for the reason gold.AS_OF is: a window that moves on its own makes two
    -- runs over the same silver incomparable. percentile over placed_at puts
    -- HOLDOUT_FRACTION of the *history* after the split, which is what a
    -- temporal holdout means.
    -- unix_timestamp() rather than a cast: Spark refuses TIMESTAMP -> DOUBLE
    -- outright, and this is the same call gold.customer_360 makes when it needs
    -- an instant as a number.
    span AS (
        SELECT percentile_approx(
            unix_timestamp(placed_at), {train_through}, 10000) AS split_at
        FROM settled
    ),
    windowed AS (
        SELECT s.order_id, s.demo_id,
               unix_timestamp(s.placed_at) <= p.split_at AS is_training
        FROM settled s CROSS JOIN span p
    ),
    -- DISTINCT because two lines of the same item built two ways is one item in
    -- that order: this is about which items turn up together, not how many of
    -- each. The same call gold.item_affinity makes.
    in_order AS (
        SELECT DISTINCT w.order_id, w.demo_id, w.is_training, i.item_id
        FROM windowed w JOIN {order_items} i ON i.order_id = w.order_id
    )
    SELECT demo_id, order_id, item_id, is_training FROM in_order"""
)

_AFFINITY = """WITH
    scoped AS (SELECT DISTINCT order_id, item_id FROM {events} WHERE {window}),
    corpus AS (SELECT count(DISTINCT order_id) AS orders FROM scoped),
    per_item AS (
        SELECT item_id, count(*) AS orders_with FROM scoped GROUP BY item_id
    ),
    pairs AS (
        SELECT a.item_id AS item_id, b.item_id AS related_item_id,
               count(*) AS co_orders
        FROM scoped a
        JOIN scoped b ON b.order_id = a.order_id AND b.item_id <> a.item_id
        GROUP BY a.item_id, b.item_id
    )
    SELECT
        p.item_id,
        p.related_item_id,
        p.co_orders,
        x.orders_with AS orders_with_item,
        y.orders_with AS orders_with_related,
        c.orders AS orders
    FROM pairs p
    JOIN per_item x ON x.item_id = p.item_id
    JOIN per_item y ON y.item_id = p.related_item_id
    CROSS JOIN corpus c
    WHERE p.co_orders >= {minimum_co_orders}"""

_PUBLISH = """SELECT
        r.demo_id,
        CAST(r.rank AS INT) AS rank,
        r.item_id,
        r.seed_item_id,
        CAST(r.seed_share AS {score_type}) AS seed_share,
        CAST(r.score AS {score_type}) AS score,
        {rationale} AS rationale,
        '{model_version}' AS model_version,
        current_timestamp() AS {derived_at}
    FROM {scored} r
    JOIN {menu_items} seed ON seed.item_id = r.seed_item_id
    JOIN {menu_items} item ON item.item_id = r.item_id"""


def training_query(
    orders: str, order_items: str, holdout_fraction: float = HOLDOUT_FRACTION
) -> str:
    """Return the SQL that produces the split, labelled order-item events.

    One row per (order, item) over the whole settled history, each carrying
    whether it falls in the training window. Both the fit and the evaluation
    read this one relation, so the split is computed once and cannot differ
    between them.

    Args:
        orders: The fully qualified ``silver_synthetic.orders``.
        order_items: The fully qualified ``silver_synthetic.order_items``.
        holdout_fraction: The share of history held back. Defaults to
            :data:`HOLDOUT_FRACTION`.

    Returns:
        One SQL statement.

    Raises:
        ValueError: If ``holdout_fraction`` is not strictly between 0 and 1. A
            holdout of nothing is not an evaluation and a holdout of everything
            is not a fit.
    """
    if not 0 < holdout_fraction < 1:
        raise ValueError(
            f"a holdout is a fraction of the history; got {holdout_fraction!r}"
        )
    return _TRAINING.format(
        orders=orders,
        order_items=order_items,
        settled=", ".join(f"'{status}'" for status in SETTLED_STATUSES),
        train_through=round(1 - holdout_fraction, 6),
    )


def affinity_query(
    events: str, *, training_only: bool, minimum_co_orders: int = MINIMUM_CO_ORDERS
) -> str:
    """Return the SQL that counts co-occurrence over the events relation.

    Run twice by ``recommender_train.py``: once over the training window, whose
    output is the model that is evaluated, and once over everything, whose
    output is the model that is registered and the one :data:`AGREEMENT`
    compares to ``item_affinity``. The same statement both times, with one
    predicate changed, so the two fits cannot differ in anything else.

    Args:
        events: The relation :func:`training_query` produced, as a name.
        training_only: Whether to count the training window only.
        minimum_co_orders: The support floor. Defaults to
            :data:`MINIMUM_CO_ORDERS`.

    Returns:
        One SQL statement, whose columns are :class:`Affinity`'s fields.

    Raises:
        ValueError: If ``minimum_co_orders`` is below one. A pair that has never
            been co-ordered has no lift to compute.
    """
    if minimum_co_orders < 1:
        raise ValueError(f"a pair needs at least one co-order; got {minimum_co_orders!r}")
    return _AFFINITY.format(
        events=events,
        window="is_training" if training_only else "true",
        minimum_co_orders=minimum_co_orders,
    )


def publish_query(scored: str, menu_items: str, model_version: str) -> str:
    """Return the SQL that renders scored rows into :data:`MART`.

    The model returns item ids and a score; the published row needs a sentence,
    and the sentence needs the two items' published names. Those live in
    ``silver_harvested.menu_items``, so the rationale is rendered here rather
    than inside the model -- which also keeps the model's own output free of
    anything harvested, so a catalogue re-harvest changes the wording without
    invalidating a model version.

    Args:
        scored: The relation holding the model's output, as a name. Its columns
            are :class:`Recommendation`'s fields plus ``demo_id``.
        menu_items: The fully qualified ``silver_harvested.menu_items``.
        model_version: The Unity Catalog model version that produced the rows.

    Returns:
        One SQL statement producing exactly :func:`column_names`, in order.

    Raises:
        ValueError: If ``model_version`` is empty. A published row that cannot
            name the version behind it is a row nobody can trace.
    """
    if not model_version:
        raise ValueError("a published recommendation names the version that made it")
    return _PUBLISH.format(
        scored=scored,
        menu_items=menu_items,
        model_version=model_version,
        score_type=SCORE,
        derived_at=DERIVED_AT,
        rationale=rationale_expression("seed.name", "r.seed_share", "item.name"),
    )
