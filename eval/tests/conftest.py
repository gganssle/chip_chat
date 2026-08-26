"""Fixtures for the labeled photo set's own tests.

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

from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.photos.testing import synthetic_set
from chip_chat.otel.testing import SpanRecorder, span_recorder


@pytest.fixture
def labels(tmp_path: Path) -> LabeledSet:
    """A complete synthetic set: thirty-one frames covering every requirement."""
    return synthetic_set(tmp_path)


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Record the spans a run emits, so the trace shape can be asserted on."""
    with span_recorder("eval") as recorder:
        yield recorder
