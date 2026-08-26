"""The two sets, promoted into one versioned dataset an experiment can be run against.

Issue #72 in one sentence: *take the golden set from Phase 2 and promote it into
a versioned dataset, so that every prompt or model change can be run against the
same thing and compared*. The emphasis in the ticket is on **versioned**, and it
gives the reason -- ``#73`` runs each change as an experiment against a fixed
dataset, and *a dataset that drifts silently makes every comparison
meaningless*.

Six modules, and the order they run in is the order to read them:

============ ================================================ =================
Module       What it holds                                    Answers
============ ================================================ =================
``entries``  One flat row per case and per frame              what is in it
``versions`` The fingerprint, and the column it rides in      which dataset
``build``    Both manifests in, one dataset out               what was built
``store``    The seam, and Arize AX behind it                 where it goes
``publish``  Create it, or add a version. Never mutate        what happened
``testing``  A store that remembers, for driving a publish    how it is tested
============ ================================================ =================

.. code-block:: python

    dataset = build_dataset(GoldenSet.load(...), LabeledSet.load(...))
    print(dataset.version)                  # the content, as twelve hex characters
    publish(dataset, arize_store_from_env())

Four things this package will not do, each of which is a way a versioned dataset
quietly stops being one:

**It will not change a published entry.** A question edited in place makes every
score taken against it before the edit a measurement of something nobody can see
any more, and nothing downstream -- not the dataset, not the experiment, not the
chart -- would say so. :func:`~chip_chat.eval.dataset.publish.publish` refuses,
and names the entries; a changed question gets a new id.

**It will not let the version be a number somebody maintains.** The version *is*
the content, hashed. Two builds of the same entries agree; any change to any
entry disagrees. Nothing to increment, and so nothing to forget.

**It will not let the repository disagree with itself.**
``eval/dataset/DATASET.json`` is committed, and ``--check`` fails when the
manifests build something else. A version that only moves when somebody
remembers to regenerate a file is not a version.

**It will not upload a set with a hole in it.** A published dataset is what
``#73``, ``#74`` and ``#75`` quote numbers from, so a PRD requirement covered by
nothing and delegated nowhere refuses the upload rather than riding along inside
it.

One thing it deliberately *does* do, which reads as a gap until the argument is
in view: a promoted photograph carries no expected tool. The labeled photo set
runs the vision lane directly, so no model ever chose to enter it and lane
selection is not a thing those rows can be scored on --
:attr:`~chip_chat.eval.dataset.entries.DatasetEntry.scores_routing` says which
rows it is, and ``eval/README.md`` is where the line between the two sets is
drawn.
"""

from chip_chat.eval.dataset.build import (
    DEFAULT_BUILD,
    DEFAULT_DATASET_NAME,
    Dataset,
    DatasetError,
    build_dataset,
    document,
)
from chip_chat.eval.dataset.entries import (
    DIGEST_COLUMN,
    DIGEST_LENGTH,
    GOLDEN_PREFIX,
    ID_COLUMN,
    PHOTOS_PREFIX,
    DatasetEntry,
    FrameTruth,
    InputKind,
    Origin,
    digest_of,
    golden_entries,
    photo_entries,
)
from chip_chat.eval.dataset.publish import Publication, PublishError, publish
from chip_chat.eval.dataset.store import (
    API_KEY_VARIABLE,
    SPACE_VARIABLE,
    ArizeDatasetStore,
    DatasetStore,
    Row,
    StoreError,
    arize_store_from_env,
)
from chip_chat.eval.dataset.versions import (
    VERSION_COLUMN,
    VERSION_LENGTH,
    fingerprint,
    rows,
)

__all__ = [
    "API_KEY_VARIABLE",
    "DEFAULT_BUILD",
    "DEFAULT_DATASET_NAME",
    "DIGEST_COLUMN",
    "DIGEST_LENGTH",
    "GOLDEN_PREFIX",
    "ID_COLUMN",
    "PHOTOS_PREFIX",
    "SPACE_VARIABLE",
    "VERSION_COLUMN",
    "VERSION_LENGTH",
    "ArizeDatasetStore",
    "Dataset",
    "DatasetEntry",
    "DatasetError",
    "DatasetStore",
    "FrameTruth",
    "InputKind",
    "Origin",
    "Publication",
    "PublishError",
    "Row",
    "StoreError",
    "arize_store_from_env",
    "build_dataset",
    "digest_of",
    "document",
    "fingerprint",
    "golden_entries",
    "photo_entries",
    "publish",
    "rows",
]
