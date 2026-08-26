"""The labeled photo set: the vision lane's ground truth, and the scorer over it.

Issue #56 in one sentence: *without it, "the photo matcher works well" is an
opinion*. Four modules, and the order they run in is the order to read them:

============ ================================================== ================
Module       What it holds                                      Answers
============ ================================================== ================
``labels``   The manifest: what a person says is in each frame  what is true
``coverage`` The ticket's scope, as checks                      is this the set
``run``      Every frame through the real lane                  what happened
``scoring``  Component P/R/F1, detection, outcomes              how well
``report``   The baseline, as Markdown                          written down
============ ================================================== ================

.. code-block:: python

    labels = LabeledSet.load(Path("eval/photos/labels.json"))
    labels.against(vocabulary)             # ground truth is held to the menu too
    runs = run_set(labels, lane)
    print(render(build_report(labels, runs, deployment=..., ...)))

Three things this package will not do, each of which is a way an evaluation
quietly stops measuring anything:

**It will not score a set it cannot believe.** A label naming a term the
catalogue does not publish, a per-meal label on a frame with two meals in it, a
required slot that is neither given nor marked unreadable -- each is refused at
load. See :mod:`chip_chat.eval.photos.labels`.

**It will not report one number.** Components are scored per slot, before and
after the confidence floors, because issue #54 shipped those floors as an
argument rather than a measurement and one aggregate F1 cannot move them. See
:mod:`chip_chat.eval.photos.scoring`.

**It will not let a good score stand in for a good set.** The scope is thirty
frames *including the hard ones*; thirty clean overhead bowls would score
beautifully and mean nothing. :mod:`chip_chat.eval.photos.coverage` is that
check, and :func:`~chip_chat.eval.photos.report.render` prints it above the
scores rather than below them.
"""

from chip_chat.eval.photos.coverage import (
    MINIMUM_PHOTOS,
    REQUIREMENTS,
    Coverage,
    Requirement,
    coverage,
)
from chip_chat.eval.photos.labels import (
    MULTI_VALUED_SLOTS,
    SINGLE_VALUED_SLOTS,
    Capture,
    Condition,
    LabeledSet,
    LabelError,
    PhotoLabel,
)
from chip_chat.eval.photos.report import Report, build_report, render
from chip_chat.eval.photos.run import (
    DEFAULT_SESSION,
    EVAL_CONTAINER,
    PhotoRun,
    PhotoSetImages,
    ref_for,
    run_set,
)
from chip_chat.eval.photos.scoring import (
    F1_TARGET,
    ComponentScore,
    DetectionScore,
    OutcomeScore,
    Scores,
    SlotScore,
    Stage,
    expected_outcome,
    score,
)

__all__ = [
    "DEFAULT_SESSION",
    "EVAL_CONTAINER",
    "F1_TARGET",
    "MINIMUM_PHOTOS",
    "MULTI_VALUED_SLOTS",
    "REQUIREMENTS",
    "SINGLE_VALUED_SLOTS",
    "Capture",
    "ComponentScore",
    "Condition",
    "Coverage",
    "DetectionScore",
    "LabelError",
    "LabeledSet",
    "OutcomeScore",
    "PhotoLabel",
    "PhotoRun",
    "PhotoSetImages",
    "Report",
    "Requirement",
    "Scores",
    "SlotScore",
    "Stage",
    "build_report",
    "coverage",
    "expected_outcome",
    "ref_for",
    "render",
    "run_set",
    "score",
]
