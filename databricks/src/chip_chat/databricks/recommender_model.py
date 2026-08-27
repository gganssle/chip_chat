"""The MLflow wrapper. Four methods, no decisions, and the only file that imports mlflow.

:mod:`chip_chat.databricks.recommender` is stdlib-only on purpose -- Terraform
uploads it beside three notebooks, MLflow logs it into every model version as
``code_paths``, and it is imported again inside whatever process ends up scoring.
A dependency there is a dependency everywhere. But a Unity Catalog model has to
*be* something, and the thing it is is an ``mlflow.pyfunc.PythonModel``.

So this file exists to hold the imports that cannot live next door, and it holds
nothing else. Every number, threshold, exclusion rule and tie-break is in
``recommender.py``; :meth:`Recommender.predict` reads the fitted pairs out of
the artifact and calls ``recommender.recommend``. If a behaviour of the model is
in question, it is not in this file.

**It is deliberately not importable in CI**, and the flat ``import recommender``
below is why. MLflow copies the files named in ``code_paths`` into a model
version as a flat directory and puts that directory on ``sys.path``, which is
also how Terraform uploads them beside the notebooks -- so on every surface that
ever loads this model, the module next door is called ``recommender`` and not
``chip_chat.databricks.recommender``. Writing the import both ways would be
writing a branch that only one side is ever exercised.

``databricks/tests/test_recommender.py`` therefore reads this file as text, the
way it and ``test_gold.py`` already read the notebooks and the Terraform, and
asserts the properties that matter: that it delegates, that it declares a
signature, and that it holds no threshold of its own.

## What is in a version

Two things, and keeping them apart is the point.

The **artifact** is the fitted pairs as JSON: for each ordered pair, the four
integers ``recommender.Affinity`` carries. Counts rather than scores, so a person
reading a logged version sees the evidence, and so that a hyperparameter sweep
can rescore an existing fit rather than refit it.

The **code** is ``recommender.py``, logged with the version. A model version that
loaded today's module would silently change behaviour when a threshold moved,
which is the failure a registry exists to prevent: the version in the registry
has to be the model that produced the metrics logged beside it.
"""

from __future__ import annotations

import json
from typing import Any

import mlflow
import pandas as pd
import recommender
from mlflow.models import ModelSignature
from mlflow.types.schema import ColSpec, Schema

__all__ = ["ARTIFACT", "Recommender", "signature"]

ARTIFACT = "affinities"
"""The artifact key holding the fitted pairs. One JSON file."""


def signature() -> ModelSignature:
    """Return the model's signature.

    Unity Catalog requires one -- a registered model without a signature cannot
    be served, and the requirement is a good one: the columns below are the
    whole interface, and writing them down is what stops a scoring job from
    discovering them by trial.

    Returns:
        The signature. Input is one row per visitor: their id, the items they
        have settled-ordered with how many of their orders contained each, and
        how many orders they placed. Output is the ranked suggestions, as JSON,
        for the reason :meth:`Recommender.predict` gives.
    """
    return ModelSignature(
        inputs=Schema(
            [
                ColSpec("string", "demo_id"),
                ColSpec("string", "history_json"),
                ColSpec("long", "orders"),
            ]
        ),
        outputs=Schema([ColSpec("string", "recommendations_json")]),
    )


class Recommender(mlflow.pyfunc.PythonModel):
    """The registered model.

    Holds the fitted pairs and calls ``recommender.recommend``. Nothing else.
    """

    def load_context(self, context: Any) -> None:
        """Read the fitted pairs out of the logged artifact.

        Args:
            context: The MLflow context, whose ``artifacts`` map carries
                :data:`ARTIFACT`.
        """
        with open(context.artifacts[ARTIFACT], encoding="utf-8") as handle:
            fitted = json.load(handle)
        self._entrees = frozenset(fitted["entrees"])
        self._by_seed: dict[str, list[Any]] = {}
        for row in fitted["pairs"]:
            pair = recommender.Affinity(**row)
            self._by_seed.setdefault(pair.item_id, []).append(pair)

    def predict(
        self, context: Any, model_input: pd.DataFrame, params: Any = None
    ) -> pd.DataFrame:
        """Return each visitor's recommendations.

        Both the history in and the recommendations out travel as JSON strings
        rather than as arrays of structs. That is a concession to the boundary
        and not to taste: a pyfunc signature over nested types has to agree with
        the Spark schema of whatever calls it, and every scoring surface -- a
        ``spark_udf``, a served endpoint, a local ``predict`` in a test --
        marshals nesting differently. One string column crosses all three the
        same way, and ``recommender_publish.py`` explodes it back into rows in
        SQL, where the shape is declared once.

        Args:
            context: Unused; the fitted pairs were read in
                :meth:`load_context`.
            model_input: One row per visitor, per :func:`signature`.
            params: Unused. Present because MLflow passes it.

        Returns:
            One row per input row, in the same order, holding a JSON array of
            objects with ``item_id``, ``seed_item_id``, ``seed_share``,
            ``score`` and ``rank``. An empty array is a visitor with nothing to
            go on, which is an honest absence rather than a fallback.
        """
        answers = []
        for record in model_input.to_dict("records"):
            history = {
                str(item): int(count)
                for item, count in json.loads(record["history_json"]).items()
            }
            pairs = [
                pair for item in sorted(history) for pair in self._by_seed.get(item, ())
            ]
            suggestions = recommender.recommend(
                history, int(record["orders"]), pairs, entrees=self._entrees
            )
            answers.append(
                json.dumps(
                    [
                        {
                            "item_id": suggestion.item_id,
                            "seed_item_id": suggestion.seed_item_id,
                            "seed_share": str(suggestion.seed_share),
                            "score": str(suggestion.score),
                            "rank": suggestion.rank,
                        }
                        for suggestion in suggestions
                    ]
                )
            )
        return pd.DataFrame({"recommendations_json": answers})
