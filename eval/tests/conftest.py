"""Fixtures for the evaluation packages' own tests.

Three sets live here. The labeled photo set's fixtures come first; the golden
set's are the committed catalogue build and the shipped manifest, both read off
disk rather than built, because what those tests are for is checking that the
set this repository commits is coherent against the catalogue this repository
commits. The third is the versioned dataset those two build, and it is a fixture
for the same reason: ``eval/dataset/DATASET.json`` is committed, so what a test
of it has to compare against is what the *manifests* build rather than what a
convenient stand-in would.

Note that ``labels`` and ``photos`` are both labeled photo sets and are not
interchangeable. ``labels`` is the synthetic thirty-one frame fixture, which is
what the scorer's arithmetic is driven at full size against; ``photos`` is the
manifest this repository ships, which holds no frames yet. A test about the
committed dataset needs the second one, and a test about promoting frames into
rows needs the first.

The synthetic set and the scripted describer ship in
:mod:`chip_chat.eval.photos.testing` rather than here, for the reason that
module's docstring gives: the arithmetic has to be driven at the size it will
really run at, and a set of thirty is a fixture two other test modules want.

The fourth set is the labeled retrieval set and its corpus, and the pair is the
same arrangement one layer down: ``retrieval_questions`` is the manifest this
repository ships and ``corpus_fixture`` is the committed 31-chunk export those
labels are resolved against. They are not interchangeable with anything above --
a retrieval label names a place in a *corpus*, and the catalogue is a different
register.

``rows`` is not a fourth set but a reading of the third: the dataset's routing
rows as expectations, which is what :mod:`chip_chat.eval.trajectory` scores a
span tree against. ``asked`` is a second reading of the same dataset -- its rows
as the questions :mod:`chip_chat.eval.grounding` scores a *response* against --
and the two are deliberately not the same fixture: a photograph is a trajectory
nothing routed to and a question nobody answered in prose, and the two evals
drop it for two different reasons.

There is deliberately **no** ``chat.turn`` fixture here, unlike
``vision/tests/conftest.py``. :func:`~chip_chat.eval.photos.run.run_set` opens
one turn per frame, and ``chat.turn`` is only permitted at the trace root -- so
a fixture that opened one would make every run in this directory raise
:class:`~chip_chat.otel.spans.SpanSchemaError`. That the schema says so is the
point; the runner's shape follows from it rather than working around it.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from chip_chat.catalog import load_catalog
from chip_chat.catalog.records import MenuCatalog
from chip_chat.eval.adversarial.attacks import (
    DEFAULT_MANIFEST as ADVERSARIAL_MANIFEST,
)
from chip_chat.eval.adversarial.attacks import AdversarialSuite
from chip_chat.eval.dataset.build import Dataset, build_dataset
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST, GoldenSet
from chip_chat.eval.grounding.questions import Question, questions
from chip_chat.eval.photos.__main__ import DEFAULT_MANIFEST as PHOTOS_MANIFEST
from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.photos.testing import synthetic_set
from chip_chat.eval.retrieval.__main__ import DEFAULT_MANIFEST as RETRIEVAL_MANIFEST
from chip_chat.eval.retrieval.questions import RetrievalSet
from chip_chat.eval.trajectory.expectations import Expectation, expectations
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.search.corpus import ChunkSet, from_path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_FIXTURE = _REPO_ROOT / "catalog" / "tests" / "fixtures"
CORPUS_RUN_ID = "20260827T053000Z"
"""What the committed chunk fixture's release is called.

The same string ``search/tests/test_retrieve.py`` builds its index under. A run
id is the corpus's identity, and two files naming the same corpus two things
would make two reports look incomparable when they are not.
"""


@pytest.fixture
def labels(tmp_path: Path) -> LabeledSet:
    """A complete synthetic set: thirty-one frames covering every requirement."""
    return synthetic_set(tmp_path)


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Record the spans a run emits, so the trace shape can be asserted on."""
    with span_recorder("eval") as recorder:
        yield recorder


@pytest.fixture(scope="session")
def catalog() -> MenuCatalog:
    """The committed catalogue fixture, as the build a set is checked against.

    ``catalog/tests/fixtures`` rather than a catalogue built here: the point of
    the term check is that it runs against a real build, and the one this
    repository commits is the only real build a test can have.
    """
    return load_catalog(LocalBlobStore(_CATALOG_FIXTURE), "catalog")


@pytest.fixture(scope="session")
def golden() -> GoldenSet:
    """The golden set that ships, loaded from its manifest."""
    return GoldenSet.load(_REPO_ROOT / DEFAULT_MANIFEST)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository root, for a test about a committed file."""
    return _REPO_ROOT


@pytest.fixture(scope="session")
def photos() -> LabeledSet:
    """The labeled photo set that ships -- empty today, and deliberately so.

    Distinct from ``labels`` above, which is the synthetic thirty-one frame
    fixture. The dataset's committed build is a function of what this
    repository *commits*, so the test that holds the build to the manifests has
    to read the manifest rather than a fixture that stands in for one.
    """
    return LabeledSet.load(_REPO_ROOT / PHOTOS_MANIFEST)


@pytest.fixture(scope="session")
def shipped(golden: GoldenSet, photos: LabeledSet) -> Dataset:
    """The versioned dataset both shipped manifests build."""
    return build_dataset(golden, photos)


@pytest.fixture(scope="session")
def rows(shipped: Dataset) -> tuple[Expectation, ...]:
    """The dataset's routing rows, as the trajectory eval reads them.

    Built from ``shipped`` rather than from the golden set, because that is the
    register #74 scores against: a trajectory number is comparable with another
    one only if both were taken against the same dataset version.
    """
    return expectations(shipped)


@pytest.fixture(scope="session")
def asked(shipped: Dataset) -> tuple[Question, ...]:
    """The dataset's rows, as the grounding eval reads them.

    Built from ``shipped`` for the reason ``rows`` is: a groundedness number is
    comparable with another one only if both were taken against the same
    dataset version.
    """
    return questions(shipped)


@pytest.fixture(scope="session")
def suite() -> AdversarialSuite:
    """The adversarial suite that ships, loaded from its manifest."""
    return AdversarialSuite.load(_REPO_ROOT / ADVERSARIAL_MANIFEST)


@pytest.fixture(scope="session")
def retrieval_questions() -> RetrievalSet:
    """The labeled retrieval set that ships, loaded from its manifest."""
    return RetrievalSet.load(_REPO_ROOT / RETRIEVAL_MANIFEST)


@pytest.fixture(scope="session")
def corpus_fixture() -> ChunkSet:
    """The committed 31-chunk corpus, read the way a build reads one.

    ``search/tests/fixtures`` rather than a corpus built here, for the reason
    ``catalog`` above is read off disk: the point of resolving a label is that
    it is resolved against a real chunk export, and the one this repository
    commits is the only real one a test can have. It is a **slice** of the
    published pages -- two of the set's labels name places it has never held --
    which is why the resolution tests assert on named ids rather than on a
    count.
    """
    return from_path(
        _REPO_ROOT / "search" / "tests" / "fixtures" / "chunks.jsonl", CORPUS_RUN_ID
    )
