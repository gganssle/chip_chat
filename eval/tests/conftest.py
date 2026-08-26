"""Fixtures for the evaluation packages' own tests.

Two sets live here. The labeled photo set's fixtures come first; the golden
set's are the committed catalogue build and the shipped manifest, both read off
disk rather than built, because what those tests are for is checking that the
set this repository commits is coherent against the catalogue this repository
commits.

The synthetic set and the scripted describer ship in
:mod:`chip_chat.eval.photos.testing` rather than here, for the reason that
module's docstring gives: the arithmetic has to be driven at the size it will
really run at, and a set of thirty is a fixture two other test modules want.

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
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST, GoldenSet
from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.photos.testing import synthetic_set
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.otel.testing import SpanRecorder, span_recorder

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_FIXTURE = _REPO_ROOT / "catalog" / "tests" / "fixtures"


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
