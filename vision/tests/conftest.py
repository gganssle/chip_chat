"""Fixtures for the upload path.

The payloads themselves ship in :mod:`chip_chat.vision.testing`, alongside the
store double, because the abuse work in issue #80 wants the same set and a
package is a better place to find them than somebody else's ``tests`` directory.
"""

from collections.abc import Iterator

import pytest

from chip_chat.otel import chat_turn
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.vision.testing import (
    ELF_BINARY,
    GZIP_ARCHIVE,
    HTML_PAGE,
    PDF_DOCUMENT,
    SHELL_SCRIPT,
    SVG_WITH_SCRIPT,
    ZIP_ARCHIVE,
)


@pytest.fixture(
    params=[
        pytest.param(ZIP_ARCHIVE, id="zip"),
        pytest.param(GZIP_ARCHIVE, id="gzip"),
        pytest.param(ELF_BINARY, id="elf"),
        pytest.param(SHELL_SCRIPT, id="shell-script"),
        pytest.param(HTML_PAGE, id="html"),
        pytest.param(SVG_WITH_SCRIPT, id="svg-with-script"),
        pytest.param(PDF_DOCUMENT, id="pdf"),
    ]
)
def disguised_payload(request: pytest.FixtureRequest) -> bytes:
    """Something that is not a photograph, whatever the request calls it."""
    payload: bytes = request.param
    return payload


@pytest.fixture(autouse=True)
def spans() -> Iterator[SpanRecorder]:
    """Run every test in this package inside a recorded ``chat.turn``.

    Two jobs, and the first one is not optional. Stage 3 emits
    ``guard.content_safety``, which RFC-001 section 09 places under
    ``chat.turn`` -- and :mod:`chip_chat.otel.spans` enforces the tree rather
    than merely documenting it, so an upload screened outside a turn raises
    :class:`~chip_chat.otel.spans.SpanSchemaError`. That is the schema working:
    the guard belongs to the turn it is protecting. This fixture is what stops
    every test in the directory from having to say so.

    The second job is that several of the acceptance criteria on issue #52 are
    stated in terms of traces, so the spans have to be somewhere a test can
    read them. Request this fixture by name to do that.

    Note that ``chat.turn`` itself closes at teardown, after the test body, so
    it is the *names* recorded during the body that a test asserts on -- which
    is the right assertion anyway, since what matters is which spans happened
    and in what order.
    """
    with (
        span_recorder("vision") as recorder,
        chat_turn(session_id="vision-tests", turn_index=0, message=""),
    ):
        yield recorder
